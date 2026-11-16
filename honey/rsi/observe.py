"""RSI observer: turns hook payloads into a per-session event log. Deterministic, no LLM, fail-silent."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time

from honey import paths
from honey.rsi import signals, store, text

WINDOW = 3  # user turns after a skill fires during which the first signal decides its outcome
QUARANTINE_TOOLS = re.compile(r"^(?:WebFetch|WebSearch|web_search|mcp__.*)$")
SKILL_READ = re.compile(r"/skills/([\w:-]{1,60})/SKILL\.md")


def events_file(key: str) -> str:
    return paths.path("events", time.strftime("%Y-%m"), f"{key}.jsonl")


def record(ctx, e: str, **fields) -> None:
    if ctx.state.get("off") or ctx.state.get("rsi_off"):
        if e != "cmd":
            return
    row = {"v": 1, "ts": text.now(), "a": ctx.agent, "sid": paths.safe_id(ctx.sid), "t": ctx.state.get("turn", 0), "e": e}
    task = (ctx.state.get("task") or {}).get("id")
    if task and "task" not in fields:
        row["task"] = task
    row.update({k: v for k, v in fields.items() if v not in (None, "", [], {})})
    store.append_jsonl(events_file(ctx.key), row)


def skill_invoked(ctx, name: str, by: str, via: str) -> None:
    name = (name or "").strip().lstrip("/$").split(':')[-1][:80]
    if not name:
        return
    record(ctx, "skill", name=name, by=by, via=via)
    pend = ctx.state.setdefault("pending_skills", [])
    task = (ctx.state.get("task") or {}).get("id")
    pend[:] = [p for p in pend if (p["name"], p.get("task")) != (name, task)]
    pend.append({"name": name, "t": ctx.state.get("turn", 0), "task": task})
    del pend[:-5]


def attribution(ctx, prompt: str, sig: list) -> dict:
    """A task match is required; explicit skill references may resolve a multi-skill task."""
    kind = signals.feedback_kind(prompt, sig)
    task = (ctx.state.get("task") or {}).get("id")
    turn = ctx.state.get("turn", 0)
    eligible = [p for p in ctx.state.get("pending_skills", [])
                if 1 <= turn - p["t"] <= WINDOW and p.get("task") == task]
    named = [p["name"].split(':')[-1] for p in eligible if signals.mentions_skill(prompt, p["name"])]
    references = signals.referenced_skills(prompt)
    choices = set(named if named or references else ([p["name"].split(':')[-1] for p in eligible] if task else []))
    if references - set(named):
        choices = set()
    selected = (next(iter(choices)) if len(choices) == 1 and kind in ("behavior", "accept")
                and not text.injection_hit(prompt) else None)
    return {"feedback_kind": kind, "attributed_skill": selected,
            "attribution": "explicit" if selected and named else "single-task" if selected else "uncertain"}


def attribute(ctx, sig: list, prompt: str = "", feedback=None) -> None:
    """Record uncertainty rather than distributing credit or blame across recent skills."""
    feedback = feedback or attribution(ctx, prompt, sig)
    turn = ctx.state.get("turn", 0)
    task = (ctx.state.get("task") or {}).get("id")
    kind = feedback["feedback_kind"]
    keep = []
    seen = set()
    for p in ctx.state.get("pending_skills", []):
        p = dict(p, name=p['name'].split(':')[-1])
        identity = (p['name'], p.get('task'))
        if identity in seen:
            continue
        seen.add(identity)
        age = turn - p["t"]
        if age < 1:
            keep.append(p)
            continue
        outcome = None
        if age > WINDOW or p.get("task") != task:
            outcome = "neutral"
        elif p["name"] == feedback["attributed_skill"]:
            outcome = "neg" if kind == "behavior" else "pos"
        elif kind not in ("none", "continue") and not feedback["attributed_skill"]:
            outcome = "uncertain"
        elif age == WINDOW:
            outcome = "neutral"
        if outcome:
            record(ctx, "skill_outcome", name=p["name"], outcome=outcome, age=age,
                   feedback_kind=kind, attribution=feedback["attribution"],
                   attributed_skill=feedback["attributed_skill"], task=p.get("task"))
        else:
            keep.append(p)
    ctx.state["pending_skills"] = keep


def user_prompt(ctx, prompt: str, sig: list) -> None:
    if ctx.state.get("off") or ctx.state.get("rsi_off"):
        return
    origin = signals.origin(prompt)
    if origin == "system":
        return
    q = text.redact(prompt, 300) if origin == "user" else None
    prev = ctx.state.get("tail") if sig and set(sig) & (signals.NEGATIVE | {"error_paste"}) else None
    feedback = attribution(ctx, prompt, sig)
    record(ctx, "prompt", origin=origin, len=len(prompt or ""), sig=sig, q=q, prev=prev,
           kw=text.keywords(q or ""), xc=True if origin == "user" and text.injection_hit(prompt) else None, **feedback)
    attribute(ctx, sig, prompt, feedback)


def user_skill_mentions(ctx, prompt: str) -> None:
    """Record explicit invocations after the dispatcher advances the current task."""
    if ctx.state.get("off") or ctx.state.get("rsi_off") or signals.origin(prompt) == "system":
        return
    for m in re.finditer(r"(?:^|\s)\$([a-z][\w-]{1,40})\b", prompt or ""):
        if ctx.agent == "codex":
            skill_invoked(ctx, m.group(1), "user", "mention")


def tool_used(ctx, payload: dict, event=None) -> None:
    if ctx.state.get("off") or ctx.state.get("rsi_off"):
        return
    from honey import metrics, tool_events
    event = tool_events.normalize(payload) if event is None else event
    tool, cmd = event["tool"], event["command"]
    delta = metrics.record(ctx.state, event)
    if not delta:
        return
    origin_task = event.get('task_id', (ctx.state.get('task') or {}).get('id'))
    fields = {"tool": tool[:60], "metrics": delta, "coverage": event["supported"], 'task': origin_task}
    loaded = (event['supported'] and event['completed'] and not event['failed']
              and origin_task == (ctx.state.get('task') or {}).get('id'))
    if QUARANTINE_TOOLS.match(tool):
        ctx.state["quarantined"] = True
        fields["xc"] = True
    if loaded and event["kind"] == "skill" and event.get("skill"):
        skill_invoked(ctx, event["skill"], "model", "Skill")
    if isinstance(cmd, str) and cmd:
        fields["cmd"] = text.redact(cmd, 160)
        if loaded and event.get('exit_code') == 0 and ctx.agent == 'codex':
            # Printed paths or failed reads do not load skill instructions.
            try:
                args = shlex.split(cmd) if not any(c in cmd for c in (';', '|', '&', '>', '<', '$', '`', '\n')) else []
            except ValueError:
                args = []
            if args and os.path.basename(args[0]) in ('cat', 'head', 'tail'):
                for arg in args[1:]:
                    match = SKILL_READ.search(arg)
                    if match:
                        skill_invoked(ctx, match.group(1), 'model', 'skill_md')
        if signals.REVERT_CMD.search(cmd) and ctx.state.get("pending_skills"):
            record(ctx, "signal", sig=["revert"])
            attribute(ctx, ["revert"])
    fp = event.get("path")
    if fp:
        fields["ext"] = os.path.splitext(fp)[1][:10]
        match = SKILL_READ.search(fp)
        if loaded and event["kind"] == "read" and match:
            skill_invoked(ctx, match.group(1), "model", "skill_md")
    record(ctx, "tool", **fields)


def spawn(args: list) -> None:
    """Fire-and-forget child in its own session so it outlives the hook (SessionEnd has a ~1.5 s budget)."""
    env = dict(os.environ, RSI_CHILD="1", HONEY_OFF="1")
    log = open(store.ensure_dir(paths.path("logs")) + "/jobs.log", "ab")
    subprocess.Popen([sys.executable, os.path.join(paths.REPO, "bin", "honey"), *args], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, close_fds=True, start_new_session=True, env=env, cwd=paths.home(),
                     # Windows: detach so the job survives the hook and opens no console window
                     creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                    | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0)
