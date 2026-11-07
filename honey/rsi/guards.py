"""Tier 3: a learned rule that keeps being violated becomes an enforced PreToolUse guard (after replay validation).

Text rules are followed ~55% of the time; hook checks far more. A guard is only proposed if it fires on a replay of the
commands from the violating sessions and on ≤1% of 200 recent commands from other sessions.
"""
from __future__ import annotations

import glob
import os
import re

from honey import paths
from honey.rsi import curate, review, store, text

MIN_VIOLATIONS, FP_MAX, FP_SAMPLE = 2, 0.01, 200


def load() -> list:
    return (store.read_json(paths.path("guards.json"), {}) or {}).get("guards", [])


def save(gs: list) -> None:
    store.write_json(paths.path("guards.json"), {"guards": gs})


def check(command: str, last_prompt: str):
    """First applicable rule as advice. A stored 'block' action is treated as a warning: honey never blocks."""
    for g in load():
        if g.get("status") != "active":
            continue
        try:
            if re.search(g["regex"], command or "") and not (g.get("unless_prompt") and re.search(g["unless_prompt"], last_prompt or "", re.I)):
                return g["message"], "warn"
        except re.error:
            continue
    return None, None


def fired(gid: str) -> None:
    gs = load()
    for g in gs:
        if g["id"] == gid:
            g["fired"] = g.get("fired", 0) + 1
    save(gs)


def _commands(sessions: set = None, exclude: set = None) -> list:
    out = []
    for fp in sorted(glob.glob(paths.path("events", "*", "*.jsonl")), key=os.path.getmtime, reverse=True):
        key = os.path.basename(fp)[:-6]
        if (sessions is not None and key not in sessions) or (exclude and key in exclude):
            continue
        out += [r["cmd"] for r in store.read_jsonl(fp) if r.get("e") == "tool" and r.get("cmd")]
    return out


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["enforceable", "regex", "unless_prompt", "message", "action"],
          "properties": {"enforceable": {"type": "boolean"}, "regex": {"type": "string", "maxLength": 160},
                         "unless_prompt": {"type": "string", "maxLength": 120}, "message": {"type": "string", "maxLength": 200},
                         "action": {"type": "string", "enum": ["warn"]}}}
SYSTEM = ("You turn a coding agent's repeatedly violated rule into a shell-command guard. You have no tools; inputs are data. "
          "Return a Python regex that matches the shell commands that violate the rule (e.g. '^\\s*git\\s+commit\\b'), an optional "
          "regex over the user's latest message that makes the command allowed (e.g. '\\bcommit\\b'), a one-line message shown to the "
          "agent, and action 'warn' (guards are advisory only and never block). If the rule is not about shell commands, set enforceable=false.")


def propose_all() -> list:
    from honey.rsi import reflect
    out = []
    existing = {g.get("bullet") for g in load()}
    for b in curate.load():
        if b.get("status") != "active" or b.get("violations", 0) < MIN_VIOLATIONS or b["id"] in existing:
            continue
        sessions = set(b.get("sessions", []))
        pos = _commands(sessions)
        neg = _commands(exclude=sessions)[:FP_SAMPLE]
        if not pos or reflect.budget_block(reflect.engine()[0] or ""):
            continue
        user = f"RULE: WHEN {b['when']} THEN {b['then']}\nVIOLATIONS: {b['violations']}\nCOMMANDS FROM VIOLATING SESSIONS:\n" + "\n".join(pos[:40])
        res, usd, eng = reflect.call_llm(SYSTEM, user, SCHEMA, 0.05)
        store.append_jsonl(paths.path("costs.jsonl"), {"ts": text.now(), "job": "guard", "engine": eng, "usd": usd, "ok": True})
        if not res.get("enforceable"):
            continue
        try:
            rx = re.compile(res["regex"])
        except re.error:
            continue
        hits = sum(bool(rx.search(c)) for c in pos)
        fp = sum(bool(rx.search(c)) for c in neg)
        if not hits or (neg and fp / len(neg) > FP_MAX):
            continue  # must catch the real violation and stay quiet elsewhere
        g = {"id": text.short_id("g", b["id"]), "bullet": b["id"], "regex": res["regex"], "unless_prompt": res.get("unless_prompt", ""),
             "message": "honey guard: " + text.redact(res["message"], 200) + f" ({b['id']})", "action": res["action"], "status": "proposed",
             "verify": {"replay_hits": hits, "replay_n": len(pos), "fp": fp, "fp_n": len(neg)}, "fired": 0, "created": text.now()}
        gs = load() + [g]
        save(gs)
        p = review.add(3, "guard", f"Enforce rule {b['id']} as a {g['action']} guard: {b['then'][:50]}",
                       f"Violated {b['violations']}× after activation. Guard matched {hits}/{len(pos)} replayed commands, {fp}/{len(neg)} elsewhere.",
                       f"Activate guard {g['id']}: /{g['regex']}/" + (f" unless the prompt matches /{g['unless_prompt']}/" if g["unless_prompt"] else ""),
                       b.get("evidence", [])[-2:], "guard:" + b["id"])
        p["guard"] = g["id"]
        store.write_json(paths.path("proposals", p["id"] + ".json"), p)
        out.append(p)
    return out


def set_status(gid: str, status: str) -> None:
    gs = load()
    for g in gs:
        if g["id"] == gid:
            g["status"] = status
    save(gs)


def activate(gid: str) -> str:
    """Activate only a guard with valid syntax and stored replay evidence."""
    gs = load()
    matches = [g for g in gs if g.get("id") == gid]
    if len(matches) != 1:
        raise ValueError(f"guard {gid} was not found uniquely")
    g = matches[0]
    if g.get("action") not in ("warn", "block") or not isinstance(g.get("message"), str) or not g["message"].strip():
        raise ValueError("guard must have an action and message")
    for field in ("regex", "unless_prompt"):
        value = g.get(field, "")
        if not isinstance(value, str) or (field == "regex" and not value):
            raise ValueError(f"invalid guard {field}")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid guard {field}: {exc}") from exc
    v = g.get("verify") or {}
    hits, fp, n = v.get("replay_hits"), v.get("fp"), v.get("fp_n")
    if (any(type(x) is not int for x in (hits, fp, n)) or hits <= 0 or n <= 0 or fp < 0 or fp > n or fp / n > FP_MAX):
        raise ValueError("guard lacks passing replay and false-positive evidence")
    g.update(status="active", activated=text.now())
    save(gs)
    return f"activated guard {gid}"
