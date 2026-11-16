"""Bounded, descriptive tool metrics. Repetition is a review signal, not proof of waste."""
from __future__ import annotations

import math
import os
import shlex

from honey.hashing import sha256

MAX_SEEN = 128
MAX_FINGERPRINTS = 64
FIELDS = ("tool_calls", "completed_calls", "failed_calls", "duration_samples", "duration_ms",
          "read_calls", "check_calls", "repeated_reads", "repeated_checks")


def _hash(value):
    return sha256(value.encode("utf-8", "replace")).hexdigest()[:24]


def _category(event):
    if event.get("kind") == "read":
        return "read"
    if event.get("kind") != "shell":
        return "mutation" if event.get("kind") in ("edit", "opaque") else None
    cmd = event.get("command") or ""
    # A shell expression can run extra commands. Never assume it was a pure read.
    if any(s in cmd for s in (";", "&&", "||", "|", ">", "<", "$", "`", "\n")):
        return "mutation"
    try:
        args = shlex.split(cmd)
    except ValueError:
        return "mutation"
    if not args:
        return None
    exe = os.path.basename(args[0])
    if exe in ("pytest", "jest", "vitest", "ruff", "mypy", "tsc"):
        return "check"
    if exe in ("npm", "pnpm", "yarn", "bun") and len(args) > 1 and (args[1] in ("test", "lint", "typecheck")
            or len(args) > 2 and args[1] == 'run' and args[2] in ('test', 'lint', 'typecheck')):
        return "check"
    if exe.startswith("python") and len(args) > 1 and (args[1] in ("-m",) and len(args) > 2 and args[2] in ("pytest", "unittest")
            or args[1].startswith("tests/") or os.path.basename(args[1]).startswith("test_")):
        return "check"
    if exe in ("cat", "head", "tail", "rg", "grep", "ls", "stat", "wc", "du", "df"):
        return "read"
    if exe == "sed" and not any(a == "-i" or a.startswith("-i") for a in args[1:]):
        return "read"
    if exe == "git" and len(args) > 1 and args[1] in ("status", "diff", "log", "show", "ls-files", "rev-parse"):
        return "read"
    return "mutation"


def record(state: dict, event: dict) -> dict:
    """Record one normalized PostToolUse event; return an aggregatable counter delta."""
    data = state.setdefault("metrics", {})
    task = state.get("task") or {}
    task_id = task.get("id")
    if data.get("task_id") != task_id or "task" not in data:
        data.update(task_id=task_id, task={}, fingerprints={})
    generation = task.get("mutation", 0)
    if data.get("mutation") != generation:
        data.update(mutation=generation, fingerprints={})
    code = event.get("exit_code")
    completed = event.get("completed") is True or type(code) is int
    failed = event.get("failed") is True or type(code) is int and code != 0
    complete_level = 2 if completed else 1
    seen = data.setdefault("seen", {})
    call = event.get("call_id")
    identity = _hash(str(call)) if call else None
    previous = seen.get(identity, {}) if identity else {}
    prior = previous.get("level", 0)
    origin_task = previous.get("task", event.get('task_id', task_id))
    if prior >= complete_level:
        return {}
    if identity:
        seen[identity] = {"level": complete_level, "task": origin_task}
        while len(seen) > MAX_SEEN:
            del seen[next(iter(seen))]
    delta = {"tool_calls": 1} if prior == 0 else {}
    if completed:
        delta["completed_calls"] = 1
        delta["failed_calls"] = int(failed)
        duration = event.get("duration_ms")
        if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
            delta.update(duration_samples=1, duration_ms=duration)
    category = _category(event)
    if category == "mutation":
        data["fingerprints"] = {}
    elif category in ("read", "check") and completed:
        plural = "reads" if category == "read" else "checks"
        delta[category + "_calls"] = 1
        if origin_task == task_id:
            key = _hash(str((task_id, event.get("cwd"), event.get("tool"), event.get("path"), event.get("command"))))
            fingerprints = data.setdefault("fingerprints", {}).setdefault(category, [])
            if key in fingerprints:
                delta["repeated_" + plural] = 1
                fingerprints.remove(key)
            fingerprints.append(key)
            del fingerprints[:-MAX_FINGERPRINTS]
    buckets = [data.setdefault("session", {})]
    if origin_task == task_id:
        buckets.append(data["task"])
    for bucket in buckets:
        for field, value in delta.items():
            bucket[field] = bucket.get(field, 0) + value
    return delta


def report(state: dict) -> dict:
    """Expose counters only; omit internal hashed identifiers and fingerprints."""
    data = state.get("metrics") or {}
    task_id = (state.get("task") or {}).get("id")
    result = {"task_id": task_id}
    for name in ("session", "task"):
        bucket = {} if name == "task" and task_id != data.get("task_id") else data.get(name) or {}
        result[name] = {field: bucket.get(field, 0) for field in FIELDS}
        result[name]["unknown_results"] = bucket.get("tool_calls", 0) - bucket.get("completed_calls", 0)
    return result
