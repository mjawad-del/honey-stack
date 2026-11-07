"""Deterministic session digest for the reflector: a numbered timeline of signals, ≤ DIGEST_MAX chars."""
from __future__ import annotations

import glob
import os

from honey import paths
from honey.rsi import curate, signals, store, text

DIGEST_MAX = 12000
LLM_WORTHY = {"correction", "still", "frustration", "error_paste", "directive"}


def session_files(key: str) -> list:
    return sorted(glob.glob(paths.path("events", "*", key + ".jsonl")))


def load_events(key: str) -> list:
    rows = []
    for f in session_files(key):
        rows.extend(store.read_jsonl(f))
    return rows


def worth_reflecting(rows: list, min_turns: int = 3) -> bool:
    turns = sum(1 for r in rows if r.get("e") == "prompt" and r.get("origin") == "user")
    sig = any(LLM_WORTHY & set(r.get("sig", [])) for r in rows if r.get("e") == "prompt")
    neg_skill = any(r.get("e") == "skill_outcome" and r.get("outcome") == "neg" for r in rows)
    fails = sum(1 for r in rows if r.get("e") == "tool_fail")
    return turns >= min_turns and (sig or neg_skill or fails >= 3)


def build(key: str, rows: list = None):
    """(digest_text, {signal_id: event}, meta). Corrections and notes are kept first if truncation is needed."""
    rows = rows if rows is not None else load_events(key)
    start = next((r for r in rows if r.get("e") == "start"), {})
    project = start.get("proj", "")
    quarantined = any(r.get("xc") for r in rows)
    turns = sum(1 for r in rows if r.get("e") == "prompt")
    state = store.read_json(paths.path("state", "sessions", key + ".json"), {}) or {}
    injected_ids = state.get("brief_ids") or []
    ledger = {b["id"]: b for b in curate.load()}
    lines, ids, priority = [], {}, []
    n = 0
    fails = {}
    last_stop = None
    for r in rows:
        e = r.get("e")
        t = r.get("t", 0)
        line = None
        if e == "prompt" and (r.get("sig") or r.get("q")):
            line = f"t{t} user/{'+'.join(r.get('sig', [])) or 'plain'} \"{r.get('q', '')}\""
            if r.get("prev"):
                line += f" | prev-assistant \"{r['prev'][-200:]}\""
            if last_stop and last_stop.get("done") and set(r.get("sig", [])) & (signals.NEGATIVE | {"error_paste"}):
                line += " [after a done-claim]"
        elif e == "skill":
            line = f"t{t} skill {r.get('name')} by={r.get('by')}"
        elif e == "skill_outcome":
            line = f"t{t} skill-outcome {r.get('name')} = {r.get('outcome')}"
        elif e == "tool_fail":
            k = (r.get("tool"), (r.get("err") or "")[:60])
            fails[k] = fails.get(k, 0) + 1
            if fails[k] == 1:
                line = f"t{t} tool_fail {r.get('tool')}: \"{r.get('err', '')}\""
        elif e == "guard":
            line = f"t{t} guard {r.get('decision')}: {r.get('reason')}"
        elif e == "receipt_gap":
            line = f"t{t} receipt gap ({r.get('kind')}, advisory, stop not blocked): {r.get('reason', '')}"
        elif e == "receipt_nag":  # legacy events from before the gate became advisory
            line = f"t{t} receipt-gate blocked a stop (legacy event, reason not recorded)"
        elif e == "note":
            line = f"t{t} user note saved {r.get('id')} [{r.get('scope')}]"
        elif e == "stop":
            last_stop = r
            if r.get("ends_q"):
                line = f"t{t} assistant ended with a question"
        elif e == "compact":
            line = f"t{t} context compacted"
        elif e == "cmd":
            line = f"t{t} command {r.get('cmd')}"
        if line:
            n += 1
            sid = f"s{n}"
            ids[sid] = r
            lines.append(f"{sid} {line}")
            if e in ("prompt", "note") and set(r.get("sig", [])) & {"correction", "note", "directive"}:
                priority.append(len(lines) - 1)
    for (tool, err), c in fails.items():
        if c > 1:
            lines.append(f"   ({tool} failed {c}× with \"{err}\")")
    head = [f"SESSION {key} project={project} turns={turns} quarantined={'yes' if quarantined else 'no'}"]
    if injected_ids:
        head.append("ACTIVE RULES INJECTED: " + " | ".join(
            f"{i} WHEN {ledger[i]['when']} THEN {ledger[i]['then']}" for i in injected_ids if i in ledger))
    kws = set()
    for r in rows:
        kws.update(r.get("kw", []) or [])
    existing = sorted((b for b in ledger.values() if b.get("status") in ("active", "candidate", "stale")),
                      key=lambda b: -len(set(b.get("keys", [])) & kws))[:12]
    if existing:
        head.append("EXISTING RULES (update/supersede these by id rather than duplicating): " + " | ".join(
            f"{b['id']} [{b['status']}] WHEN {b['when']} THEN {b['then']}" for b in existing))
    never = store.read_jsonl(paths.path("never.jsonl"))[-20:]
    if never:
        head.append("DO-NOT-PROPOSE: " + " | ".join(text.redact(x.get("text", ""), 120) for x in never))
    head.append("TIMELINE")
    body = "\n".join(head + lines)
    if len(body) > DIGEST_MAX:
        keep = [lines[i] for i in priority]
        rest = [l for i, l in enumerate(lines) if i not in set(priority)]
        budget = DIGEST_MAX - len("\n".join(head + keep)) - 40
        tail = []
        for l in reversed(rest):
            if budget - len(l) - 1 < 0:
                break
            tail.insert(0, l)
            budget -= len(l) + 1
        body = "\n".join(head + keep + ["… (earlier events truncated)"] + tail)
    return body[:DIGEST_MAX], ids, {"project": project, "quarantined": quarantined, "turns": turns,
                                     "upto": len(rows), "agent": key.split("-", 1)[0]}
