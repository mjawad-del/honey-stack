"""Out-of-band reflection: digest → headless LLM (no tools) → validated JSON → deterministic curate.

Runs detached after SessionEnd and as catch-up at the next SessionStart. Budgets are checked before every paid call.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
import time

from honey import paths
from honey.rsi import curate, digest, store, text

HERE = os.path.dirname(os.path.abspath(__file__))
BUDGETS = {"reflection_usd": 0.15, "weekly_usd": 1.50, "monthly_usd": 5.00, "max_reflections_per_day": 8,
           "max_failed_reflections_per_day": 8, "codex_runs_per_week": 20}
MAX_PER_RUN = 3
MAX_FAILURES = 3  # consecutive failed reflections before a session waits for new events
IDLE_HOURS = 6


def budgets() -> dict:
    cfg = store.read_json(paths.path("config.json"), {}) or {}
    return dict(BUDGETS, **(cfg.get("budgets") or {}))


def dollar_caps_enabled() -> bool:
    """Only an explicit opt-out removes dollar limits; cadence remains bounded."""
    return budgets().get("enabled") is not False


def _spent(costs, days) -> float:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    return sum(c.get("usd") or 0 for c in costs if c.get("ts", "") >= cutoff)


def budget_block(engine: str):
    """Reason string if a paid call now would exceed a cap, else None."""
    b, costs = budgets(), store.read_jsonl(paths.path("costs.jsonl"))
    today = time.strftime("%Y-%m-%d", time.gmtime())
    today_reflects = [c for c in costs if c.get("ts", "").startswith(today) and c.get("job") == "reflect"]
    # Failed calls (e.g. an account error) must not use up the day's successful reflections.
    if sum(1 for c in today_reflects if c.get("ok") is not False) >= b["max_reflections_per_day"]:
        return "daily reflection cap"
    if sum(1 for c in today_reflects if c.get("ok") is False) >= b["max_failed_reflections_per_day"]:
        return "daily reflection failure cap"
    if dollar_caps_enabled() and _spent(costs, 7) + b["reflection_usd"] > b["weekly_usd"]:
        return "weekly budget"
    if dollar_caps_enabled() and _spent(costs, 30) + b["reflection_usd"] > b["monthly_usd"]:
        return "monthly budget"
    if engine == "codex" and sum(1 for c in costs if c.get("engine") == "codex" and c.get("ts", "") >= time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7 * 86400))) >= b["codex_runs_per_week"]:
        return "weekly codex run cap"
    return None


def engines() -> list:
    """Installed model CLIs in preference order: [(name, path), ...]."""
    found = []
    for name in ("claude", "codex"):
        for c in (os.path.expanduser(f"~/.local/bin/{name}"), f"/opt/homebrew/bin/{name}", shutil.which(name)):
            if c and os.path.exists(c):
                found.append((name, c))
                break
    return found


def engine() -> tuple:
    found = engines()
    return found[0] if found else (None, None)


def _child_env() -> dict:
    return dict(os.environ, RSI_CHILD="1", HONEY_OFF="1", HONEY_DISABLE="1")


def call_llm(system: str, user: str, schema: dict, max_usd: float, model: str = "haiku", only: str = None):
    """(structured_output dict, usd, engine) using Claude if installed, else Codex (or just `only`). Raises on failure."""
    if os.environ.get("HONEY_FAKE_LLM"):  # tests: canned reflector output, no paid call
        with open(os.environ["HONEY_FAKE_LLM"]) as f:
            return json.load(f), 0.0, "fake"
    found = [e for e in engines() if only in (None, e[0])]
    if not found:
        raise RuntimeError(f"no {only or 'claude or codex'} CLI found")
    name, exe = found[0]
    if name == "claude":
        cmd = [exe, "-p", "--model", model, "--tools", "", "--settings", '{"disableAllHooks":true}',
               "--no-session-persistence", "--strict-mcp-config", "--system-prompt", system,
               "--output-format", "json", "--json-schema", json.dumps(schema)]
        if dollar_caps_enabled():
            cmd += ["--max-budget-usd", str(max_usd)]
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=180, env=_child_env(), cwd=paths.home())
        out = json.loads(r.stdout or "{}")
        if r.returncode != 0 or out.get("is_error"):
            raise RuntimeError(f"claude -p failed: {(r.stderr or r.stdout)[-300:]}")
        so = out.get("structured_output")
        if so is None:
            so = json.loads(out.get("result") or "{}")
        return so, out.get("total_cost_usd"), "claude"
    with tempfile.TemporaryDirectory() as d:
        sf, of = os.path.join(d, "schema.json"), os.path.join(d, "out.json")
        with open(sf, "w") as f:
            json.dump(schema, f)
        cmd = [exe, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--disable", "hooks",
               "-s", "read-only", "-C", d, "--output-schema", sf, "-o", of, "-"]
        r = subprocess.run(cmd, input=system + "\n\n---\n\n" + user, capture_output=True, text=True, timeout=240, env=_child_env())
        if r.returncode != 0 or not os.path.exists(of):
            raise RuntimeError(f"codex exec failed: {(r.stderr or r.stdout)[-300:]}")
        with open(of) as f:
            return json.loads(f.read()), None, "codex"


def _processed() -> dict:
    done = {}
    for line in (open(paths.path("processed.log")).read().splitlines() if os.path.exists(paths.path("processed.log")) else []):
        parts = line.split("\t")
        if len(parts) >= 4:
            done[parts[1]] = int(parts[3]) if parts[3].isdigit() else 0
    return done


def _failures() -> dict:
    """Consecutive failed reflections per session; deferrals neither count nor reset."""
    out = {}
    for line in (open(paths.path("processed.log")).read().splitlines() if os.path.exists(paths.path("processed.log")) else []):
        parts = line.split("\t")
        if len(parts) >= 3 and not parts[2].startswith("deferred:"):
            out[parts[1]] = out.get(parts[1], 0) + 1 if parts[2].startswith(("failed:", "error:")) else 0
    return out


def _mark(key, status, upto, n_cand=0, usd=None) -> None:
    store.ensure_dir(paths.home())
    with open(paths.path("processed.log"), "a", encoding="utf-8") as f:
        f.write(f"{text.now()}\t{key}\t{status}\t{upto}\t{n_cand}\t{usd if usd is not None else ''}\n")


def due_sessions() -> list:
    """Sessions that ended (or went idle) with new events since their last processing, oldest first."""
    done = _processed()
    out = []
    for fp in sorted(glob.glob(paths.path("events", "*", "*.jsonl")), key=os.path.getmtime):
        key = os.path.basename(fp)[:-6]
        state = store.read_json(paths.path("state", "sessions", key + ".json"), {}) or {}
        if state.get("off") or state.get("rsi_off"):
            continue
        if key in [k for k, _ in out]:
            continue
        rows = digest.load_events(key)
        ended = any(r.get("e") == "end" for r in rows) or time.time() - os.path.getmtime(fp) > IDLE_HOURS * 3600
        if ended and len(rows) > done.get(key, 0):
            out.append((key, rows))
    failures = _failures()
    out.sort(key=lambda item: failures.get(item[0], 0))  # stable: failing sessions go behind fresh ones
    return out


def reflect_call(system: str, body: str, schema: dict, max_usd: float, key: str = ""):
    """Try each installed engine in order; record each failed attempt. Raises the last error if all fail."""
    candidates, last = engines() or [(None, None)], None
    for i, (name, _) in enumerate(candidates):
        if i and budget_block(name or ""):
            break
        try:
            return call_llm(system, body, schema, max_usd, only=name)
        except Exception as e:
            last = e
            store.append_jsonl(paths.path("costs.jsonl"), {"ts": text.now(), "job": "reflect", "engine": name,
                                                           "usd": None, "sid": key, "ok": False})
    raise last


def reflect_session(key: str, rows: list) -> str:
    upto = len(rows)
    prev = _processed().get(key, 0)
    new_rows = rows[prev:] if prev and prev < upto else rows
    if not digest.worth_reflecting(new_rows):
        curate.observe_only(key, new_rows)
        _mark(key, "skipped:no-signal", upto)
        return "skipped"
    name, _ = engine()
    blocked = budget_block(name or "")
    if blocked:
        _mark(key, f"deferred:{blocked}", prev)
        return "deferred"
    body, ids, meta = digest.build(key, new_rows)
    store.write_json(paths.path("digests", key + ".json"), {"digest": body, "meta": meta})
    with open(os.path.join(HERE, "prompts", "reflector.md"), encoding="utf-8") as f:
        system = f.read()
    with open(os.path.join(HERE, "schemas", "reflection.schema.json"), encoding="utf-8") as f:
        schema = json.load(f)
    try:
        result, usd, eng = reflect_call(system, body, schema, budgets()["reflection_usd"], key)
    except Exception as e:
        reason = str(e)[:60].replace("\t", " ").replace("\n", " ")
        if _failures().get(key, 0) + 1 >= MAX_FAILURES:
            _mark(key, "retired:failed:" + reason, upto)  # wait for new events instead of retrying forever
        else:
            _mark(key, "failed:" + reason, prev)
        return "failed"
    store.append_jsonl(paths.path("costs.jsonl"), {"ts": text.now(), "job": "reflect", "engine": eng, "model": "haiku",
                                                   "usd": usd, "sid": key, "ok": True})
    curate.observe_only(key, new_rows)
    stats = curate.apply_reflection(result, key, ids, meta)
    _mark(key, "ok", upto, stats.get("candidates", 0), usd)
    return "ok"


def run_pending() -> None:
    with store.lock(blocking=False) as got:
        if not got:
            return
        curate.drain_pending_notes()
        for key, rows in due_sessions()[:MAX_PER_RUN]:
            try:
                reflect_session(key, rows)
            except Exception as e:  # one bad session must not block the rest
                _mark(key, "error:" + str(e)[:60].replace("\t", " "), 0)
        curate.maintain()
        from honey.rsi import autonomy, improve
        improve.run()
        autonomy.run()
        curate.write_brief_md()
        store.git_commit("rsi: reflect + curate")
