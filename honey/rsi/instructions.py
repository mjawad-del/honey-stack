"""Tier 1: proposals that edit ~/.claude/CLAUDE.md or ~/.codex/AGENTS.md. Applied after evidence validation under the configured review policy.

Additions go inside a managed block; removals of the user's own lines must match the line exactly. Every apply stores the
file's previous text in the proposal so `honey rsi revert <id>` restores it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from honey import paths
from honey.rsi import curate, review, store, text

BEGIN, END = "<!-- honey:rsi:begin -->", "<!-- honey:rsi:end -->"
HERE = os.path.dirname(os.path.abspath(__file__))
GRADUATE_DAYS, GRADUATE_HELPFUL = 30, 3


def target(which: str) -> str:
    if which not in ("claude", "codex"):
        raise ValueError("instruction target must be claude or codex")
    return os.path.join(paths.codex_dir(), "AGENTS.md") if which == "codex" else os.path.join(paths.claude_dir(), "CLAUDE.md")


def _read(fp):
    try:
        with open(fp, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def add_to_block(doc: str, line: str) -> str:
    if BEGIN in doc and END in doc:
        head, rest = doc.split(BEGIN, 1)
        body, tail = rest.split(END, 1)
        lines = [l for l in body.strip("\n").splitlines() if l.strip()]
        if line not in lines:
            lines.append(line)
        return head + BEGIN + "\n" + "\n".join(lines) + "\n" + END + tail
    sep = "" if doc.endswith("\n") or not doc else "\n"
    return doc + sep + f"\n## Learned by honey RSI\n{BEGIN}\n{line}\n{END}\n"


def graduate() -> list:
    """Long-proven bullets → proposal to make them permanent instructions (frees brief budget)."""
    out = []
    for b in curate.load():
        if (b.get("status") == "active" and b.get("helpful", 0) >= GRADUATE_HELPFUL and b.get("severity", 1) >= 2
                and b.get("activated") and curate._days(b["activated"]) >= GRADUATE_DAYS and b.get("scope") in ("global", "user", "agent:claude", "agent:codex")):
            which = "codex" if b["scope"] == "agent:codex" else "claude"
            line = curate.line(b).split(" [b-")[0]
            out.append(review.add(1, "instr_line", f"Make permanent in {os.path.basename(target(which))}: {b['then'][:60]}",
                                  f"Active {int(curate._days(b['activated']))} days, helpful {b['helpful']}×, never harmful > helpful.",
                                  f"Add the line to the managed block of {target(which)}; the brief then drops it.",
                                  b.get("evidence", [])[-2:], f"graduate:{b['id']}"))
            p = out[-1]
            if "edit" not in p:
                p.update(edit={"file": which, "action": "add", "line": line, "bullet": b["id"]})
                store.write_json(paths.path("proposals", p["id"] + ".json"), p)
    return out


def llm_audit() -> list:
    """Weekly: find instruction lines that active learnings contradict or that keep drawing corrections."""
    from honey.rsi import reflect
    active = [b for b in curate.load() if b.get("status") == "active"]
    docs = {w: _read(target(w)) for w in ("claude", "codex")}
    if not active or not any(docs.values()) or reflect.budget_block(reflect.engine()[0] or ""):
        return []
    schema = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {"type": "array", "maxItems": 5, "items": {
        "type": "object", "additionalProperties": False, "required": ["file", "action", "line", "replacement", "reason", "bullet_ids"],
        "properties": {"file": {"type": "string", "enum": ["claude", "codex"]}, "action": {"type": "string", "enum": ["remove", "edit"]},
                       "line": {"type": "string", "maxLength": 400}, "replacement": {"type": "string", "maxLength": 400},
                       "reason": {"type": "string", "maxLength": 240}, "bullet_ids": {"type": "array", "maxItems": 4, "items": {"type": "string"}}}}}}}
    with open(os.path.join(HERE, "prompts", "audit.md"), encoding="utf-8") as f:
        system = f.read()
    user = "ACTIVE LEARNINGS:\n" + "\n".join(f"{b['id']} WHEN {b['when']} THEN {b['then']} (helpful {b.get('helpful',0)}, violations {b.get('violations',0)})" for b in active)
    for w, d in docs.items():
        user += f"\n\nFILE {w}:\n" + d[:12000]
    result, usd, eng = reflect.call_llm(system, user, schema, 0.05)
    store.append_jsonl(paths.path("costs.jsonl"), {"ts": text.now(), "job": "audit", "engine": eng, "usd": usd, "ok": True})
    out = []
    for it in (result or {}).get("items", []):
        bullet_ids = it.get("bullet_ids") or []
        if not bullet_ids or any(ident not in {b["id"] for b in active} for ident in bullet_ids):
            continue
        line = it.get("line", "").rstrip("\n")
        doc = docs.get(it.get("file"), "")
        if not line or doc.count(line) != 1 or text.injection_hit(it.get("replacement", "")):
            continue  # must quote one exact existing line
        p = review.add(1, "instr_" + it["action"], f"{it['action'].title()} a line in {os.path.basename(target(it['file']))}",
                       text.redact(it.get("reason", ""), 240), f"{it['action']}: \"{line[:120]}\"" + (f" → \"{it['replacement'][:120]}\"" if it["action"] == "edit" else ""),
                       [{"q": f"contradicted by {', '.join(it.get('bullet_ids', []))}"}], f"audit:{it['file']}:{line}")
        if "edit" not in p:
            p["edit"] = {"file": it["file"], "action": it["action"], "line": line, "replacement": it.get("replacement", "")}
        if p.get("status") == "pending" and (p.get("edit") or {}).get("action") == it["action"]:
            p["bullet_ids"] = bullet_ids
            store.write_json(paths.path("proposals", p["id"] + ".json"), p)
        out.append(p)
    return out


def in_managed_block(p: dict) -> bool:
    """True only if the quoted line sits inside honey's own managed block (never a user-written line)."""
    e = p.get("edit") if isinstance(p, dict) else None
    if not isinstance(e, dict) or e.get("file") not in ("claude", "codex"):
        return False
    inside = False
    for raw in _read(target(e["file"])).splitlines():
        if raw == BEGIN:
            inside = True
        elif raw == END:
            inside = False
        elif raw == e.get("line"):
            return inside
    return False


def apply(p: dict) -> str:
    if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", p["id"]):
        raise ValueError("instruction proposal has no valid id")
    e = p.get("edit")
    if not isinstance(e, dict) or e.get("action") not in ("add", "edit", "remove"):
        raise ValueError("proposal has no supported instruction edit")
    fp = target(e.get("file"))
    line = e.get("line")
    if not isinstance(line, str) or not line.strip() or "\n" in line or "\r" in line or line in (BEGIN, END):
        raise ValueError("instruction must quote a nonempty single line")
    replacement = e.get("replacement")
    if e["action"] == "edit" and (not isinstance(replacement, str) or not replacement.strip()
                                  or "\n" in replacement or "\r" in replacement):
        raise ValueError("instruction replacement must be a nonempty single line")
    if text.injection_hit(line if e["action"] == "add" else replacement or ""):
        raise ValueError("instruction rejected by injection scan")
    doc = _read(fp)
    if e["action"] == "add":
        if not line.lstrip("- ").strip():
            raise ValueError("instruction bullet must have text")
        if doc.count(BEGIN) != doc.count(END) or doc.count(BEGIN) > 1 or (BEGIN in doc and doc.index(END) < doc.index(BEGIN)):
            raise ValueError("instruction managed block is malformed")
        new = add_to_block(doc, "- " + line.lstrip("- "))
    else:
        lines = doc.splitlines(keepends=True)
        matches = [i for i, existing in enumerate(lines) if existing.rstrip("\r\n") == line]
        if len(matches) != 1:
            raise ValueError(f"the line no longer appears exactly once in {fp}; skipping")
        i = matches[0]
        ending = lines[i][len(line):]
        lines[i] = replacement + ending if e["action"] == "edit" else ""
        new = "".join(lines)
    p["before"] = doc
    p["after_sha"] = hashlib.sha256(new.encode("utf-8")).hexdigest()
    store.write_text(fp, new)
    store.write_json(paths.path("proposals", p["id"] + ".json"), p)
    if e.get("bullet"):
        rows = curate.load()
        for b in rows:
            if b["id"] == e["bullet"]:
                b.update(status="archived", updated=text.now(), why=(b.get("why", "") + " [graduated to instructions]")[:200])
        curate.save(rows)
    return f"updated {fp}"


def revert(pid: str) -> str:
    fp_p = paths.path("proposals", pid + ".json")
    p = store.read_json(fp_p)
    if not p or "before" not in p:
        raise SystemExit(f"{pid} has nothing to revert")
    fp = target((p.get("edit") or {}).get("file"))
    if not p.get("after_sha") or hashlib.sha256(_read(fp).encode("utf-8")).hexdigest() != p["after_sha"]:
        p["revert_conflict"] = {"ts": text.now(), "reason": "instruction file changed after application"}
        store.write_json(fp_p, p)
        raise ValueError(f"{fp} changed after {pid}; refusing to overwrite later edits")
    store.write_text(fp, p["before"])
    p.update(status="reverted", decided=text.now())
    store.write_json(fp_p, p)
    return f"restored {fp} to its text before {pid}"
