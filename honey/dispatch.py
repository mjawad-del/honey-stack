"""Single hook dispatcher for Claude Code and Codex. One frozen command line per event; all logic lives here.

Fail-open: any exception is logged to ~/.honey/logs/hook-errors.log and the hook exits 0 silently.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import traceback

from honey import autoupdate, health, hookio, modes, paths, receipt, tasks, tool_events
from honey.rsi import curate, observe, review, signals, store, text

EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "ApplyPatch"}
_RSI_CMD = re.compile(r"^[/$]?(?:honey:)?rsi\s+(off|on)[.!\s]*$", re.I)


class Ctx:
    def __init__(self, agent, payload):
        self.agent = agent
        self.payload = payload
        self.event = payload.get("hook_event_name", "")
        self.sid = payload.get("session_id", "") or ""
        self.key = paths.session_key(agent, self.sid)
        self.sub = bool(payload.get("agent_id"))
        self.state_file = paths.path("state", "sessions", self.key + ".json")
        self.state = store.read_json(self.state_file, None) or {}
        self.dirty = False


def _init_state(ctx) -> None:
    if "started" in ctx.state:
        mode = modes.selected(ctx.state, modes.defaults()["mode"])
        if ctx.state.get("mode") != mode or any(key in ctx.state for key in ("scope", "process", "voice")):
            ctx.state["mode"] = mode
            for key in ("scope", "process", "voice"):
                ctx.state.pop(key, None)
            ctx.dirty = True
        return
    ctx.state.update(modes.defaults())
    ctx.state.update(started=text.now(), turn=0, agent=ctx.agent,
                     project=paths.project_slug(ctx.payload.get("cwd", "")))
    ctx.dirty = True


def session_start(ctx) -> dict:
    _init_state(ctx)
    src = ctx.payload.get("source", "startup")
    if src == "startup" or "brief" not in ctx.state:
        cfg = store.read_json(paths.path("config.json"), {}) or {}
        rate = float((cfg.get("autonomy") or {}).get("holdout_rate", 0) or 0)
        ctx.state["holdout"] = bool(rate) and random.random() < rate  # causal check: some sessions get no brief
        ctx.state["brief"], ctx.state["brief_ids"] = ("", []) if ctx.state["holdout"] else curate.brief(ctx.agent, ctx.state["project"])
        ctx.dirty = True
    observe.record(ctx, "start", src=src, model=ctx.payload.get("model"), proj=ctx.state.get("project"),
                   holdout=ctx.state.get("holdout") or None)
    if ctx.state.get("off"):
        return hookio.context("SessionStart", 'honey-stack paused for this session (say "honey on" to resume).')
    body = modes.render(ctx.agent, ctx.state)
    if src == "compact":
        body = modes.COMPACT_PREFIX + body
    parts = [body]
    if ctx.state.get("brief"):
        parts.append(ctx.state["brief"])  # frozen per session: byte-identical on resume/compact (prompt cache)
    notice = review.notice()
    if notice:
        parts.append(notice)
    updated = autoupdate.notice()
    if updated:
        parts.append(updated)
    out = "\n\n".join(parts)
    observe.record(ctx, "inject", by="honey", hook="SessionStart", chars=len(out))
    if src == "startup" and autoupdate.due():
        try:
            observe.spawn(["autoupdate"])  # detached; takes effect next session
        except Exception:
            pass  # an update job must never cost the user their SessionStart context
    if src == "startup" and not ctx.state.get("rsi_off"):
        _prune_states()
        if os.path.exists(paths.path("events")):
            jobs = store.read_json(paths.path("state", "jobs.json"), {}) or {}
            if time.time() - jobs.get("weekly", 0) > 7 * 86400 and jobs.get("first_seen") and time.time() - jobs["first_seen"] > 7 * 86400:
                observe.spawn(["rsi", "weekly"])
            else:
                observe.spawn(["rsi", "catchup"])  # sessions whose SessionEnd never fired (crash, closed window)
            if not jobs.get("first_seen"):
                store.write_json(paths.path("state", "jobs.json"), dict(jobs, first_seen=time.time()))
    return hookio.context("SessionStart", out)


def _prune_states() -> None:
    d = paths.path("state", "sessions")
    try:
        cutoff = time.time() - 14 * 86400
        for n in os.listdir(d):
            fp = os.path.join(d, n)
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
    except OSError:
        pass


def user_prompt(ctx) -> dict:
    _init_state(ctx)
    prompt = ctx.payload.get("prompt", "") or ""
    org = signals.origin(prompt)
    if org == "system":
        ctx.state["sys_prompts"] = ctx.state.get("sys_prompts", 0) + 1
        ctx.dirty = True
        return {}  # harness-injected text is never treated as the user talking
    m = _RSI_CMD.match(prompt.strip())
    if m:
        ctx.state["rsi_off"] = m.group(1).lower() == "off"
        ctx.dirty = True
        observe.record(ctx, "cmd", cmd=f"rsi {m.group(1).lower()}")
        return hookio.context("UserPromptSubmit", "honey RSI " + ("paused for this session (say `rsi on` to resume)." if ctx.state["rsi_off"] else "observing again.")
                              + " Acknowledge in one short line.")
    args = modes.parse_command(prompt)
    if args:
        ctx.dirty = True
        observe.record(ctx, "cmd", cmd="honey " + " ".join(args))
        return hookio.context("UserPromptSubmit", modes.apply_command(ctx.state, args))
    if ctx.state.get("off"):
        return {}
    ctx.state["turn"] = ctx.state.get("turn", 0) + 1
    ctx.state["edited"] = ctx.state["nagged"] = False
    ctx.dirty = True
    ctx.state["last_prompt"] = text.redact(prompt, 300)
    sig = signals.classify(prompt) if org == "user" else []
    observe.user_prompt(ctx, prompt, sig)
    tasks.advance(ctx, prompt)
    observe.user_skill_mentions(ctx, prompt)
    out = []
    if "note" in sig:
        note = signals.parse_note(prompt)
        b, merged = curate.add_note(note["text"], note["scope"], ctx.agent, ctx.sid, ctx.state.get("project", ""))
        if b:
            observe.record(ctx, "note", id=b["id"], scope=b["scope"])
            verb = "queued (saved when the running RSI job finishes)" if merged == "queued" else \
                "saved (merged into)" if merged else "saved"
            out.append(f"honey RSI {verb} learning {b['id']} [{b['scope']}]: "
                       f"{curate.line(b)[2:]}. Acknowledge in one short line, then handle anything else in the message.")
        else:
            out.append(f"honey RSI did not save that note ({merged}). Tell the user in one line.")
    return hookio.context("UserPromptSubmit", "\n".join(out)) if out else {}


def prompt_expansion(ctx) -> dict:
    _init_state(ctx)
    name = ctx.payload.get("command_name", "")
    if ctx.payload.get("command_source") in ("plugin", "skill", "user", "project", None):
        observe.skill_invoked(ctx, name, "user", "expansion")
        ctx.dirty = True
    return {}


def subagent_start(ctx) -> dict:
    if ctx.state.get("off"):
        return {}
    return hookio.context("SubagentStart", modes.SUBAGENT)


def pre_tool(ctx) -> dict:
    event = tool_events.normalize(ctx.payload)
    if not ctx.sub:
        tasks.start_tool(ctx, event)
    if event['kind'] != 'shell':
        return {}
    cmd = event['command']
    from honey import guard
    reason, sev = guard.check(cmd)
    if not reason:
        from honey.rsi import guards as learned
        msg, action = learned.check(cmd if isinstance(cmd, str) else str(cmd), ctx.state.get("last_prompt", ""))
        if not msg:
            return {}
        if not ctx.sub:
            _init_state(ctx)
            observe.record(ctx, "guard", reason=msg[:120], decision="warn", learned=True)
        # Honey never restricts permissions: learned rules are advice only. Hosts without
        # advisory PreToolUse context (Codex) get nothing; the event is still recorded above.
        if ctx.agent == "codex":
            return {}
        return hookio.context("PreToolUse", msg)
    if not ctx.sub:
        _init_state(ctx)
        observe.record(ctx, "guard", reason=reason[:120], decision="warn", severity=sev)
    if ctx.agent == "codex":
        return {}
    return hookio.context("PreToolUse", "honey guard (advisory): " + reason
                          + " Proceed only if this is what the user intends.")


def post_tool(ctx) -> dict:
    event = tool_events.normalize(ctx.payload)
    start = ctx.state.get('tool_starts', {}).get(event.get('call_id'))
    if start:
        event['task_id'] = start['task']
    tool = event['tool']
    ti = ctx.payload.get("tool_input") or {}
    is_patch = "*** Begin Patch" in (event.get('command') or '')[:200000]
    out = {}
    if event['kind'] == 'edit' or is_patch or tasks.mutation(event):
        _init_state(ctx)
        if not ctx.sub:
            ctx.state["edited"] = True
            ctx.dirty = True
        from honey import secrets
        scan_payload = dict(ctx.payload, tool_name=tool)
        if is_patch:
            scan_payload.update(tool_name='apply_patch', tool_input={'patch': event['command']})
        reason = secrets.scan(scan_payload)
        if reason:
            out = hookio.context("PostToolUse", reason)
    if not ctx.sub:
        _init_state(ctx)
        tasks.record_tool(ctx, event)
        observe.tool_used(ctx, ctx.payload, event)
        ctx.dirty = True
        if tool == "Skill" and isinstance(ti, dict) and not out:
            notes = curate.skill_notes(ti.get("skill", ""))
            if notes:
                out = hookio.context("PostToolUse", notes)
    return out


def stop(ctx) -> dict:
    _init_state(ctx)
    msg = ctx.payload.get("last_assistant_message") or ""
    st = signals.classify_stop(msg)
    ctx.state["tail"] = text.redact(msg[-300:], 300)
    ctx.dirty = True
    observe.record(ctx, "stop", len=len(msg), done=st["claims_done"] or None, ends_q=st["ends_q"] or None,
                   edited=ctx.state.get("edited") or None)
    gap = receipt.check(ctx.payload, ctx.state)
    if gap:
        observe.record(ctx, "receipt_gap", kind=gap[0], reason=gap[1][:200])
    return {}  # a stop is never blocked


def session_end(ctx) -> dict:
    _init_state(ctx)
    observe.record(ctx, "end", reason=ctx.payload.get("reason"))
    if not ctx.state.get("off") and not ctx.state.get("rsi_off"):
        observe.spawn(["rsi", "session-end", ctx.key])
    return {}


def passive(ctx) -> dict:
    if not ctx.sub:
        _init_state(ctx)
        name = {"PostToolUseFailure": "tool_fail", "PostCompact": "compact", "PreCompact": "compact", "Interrupt": "interrupt"}.get(ctx.event, ctx.event)
        extra = {}
        if ctx.event == "PostToolUseFailure":
            event = tool_events.normalize(ctx.payload)
            start = ctx.state.get('tool_starts', {}).get(event.get('call_id'))
            if start:
                event['task_id'] = start['task']
            tasks.record_tool(ctx, event)
            observe.tool_used(ctx, ctx.payload, event)
            extra = {"tool": ctx.payload.get("tool_name"), "err": text.redact(str(ctx.payload.get("error", "")), 160)}
        elif ctx.event in ("PostCompact", "PreCompact"):
            extra = {"trigger": ctx.payload.get("trigger")}
        observe.record(ctx, name, **extra)
    return {}


HANDLERS = {"SessionStart": session_start, "UserPromptSubmit": user_prompt, "UserPromptExpansion": prompt_expansion,
            "SubagentStart": subagent_start, "PreToolUse": pre_tool, "PostToolUse": post_tool, "Stop": stop,
            "SessionEnd": session_end}


def handle(agent: str, payload: dict) -> dict:
    file = paths.path('state', 'sessions', paths.session_key(agent, payload.get('session_id', '')) + '.json')
    with store.session_lock(file):
        ctx = Ctx(agent, payload)
        out = HANDLERS.get(ctx.event, passive)(ctx)
        health.record(ctx)
        if ctx.dirty:
            store.write_json(ctx.state_file, ctx.state)
        return out or {}


def run(agent: str) -> int:
    if os.environ.get("HONEY_DISABLE") == "1" or os.environ.get("RSI_CHILD") == "1":
        return 0
    try:
        payload = hookio.read_payload()
        hookio.emit(handle(agent, payload))
    except Exception:
        try:
            store.ensure_dir(paths.path("logs"))
            with open(paths.path("logs", "hook-errors.log"), "a", encoding="utf-8") as f:
                f.write(f"{text.now()} {agent}\n{traceback.format_exc()[-2000:]}\n")
        except Exception:
            pass
    return 0
