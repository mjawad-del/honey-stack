"""Ledger of learned bullets: deterministic merges, promotion, decay, budgets and the session brief.

Never lets an LLM rewrite the ledger wholesale (ACE "context collapse"); every change is an itemized op.
"""
from __future__ import annotations

import calendar
import os
import re
import time

from honey import metrics, paths
from honey.rsi import signals, store, text

BRIEF_MAX = 1500
MAX_ACTIVE_GLOBAL, MAX_ACTIVE_PROJECT, MAX_SKILL_LINES = 25, 10, 12
STALE_DAYS, ARCHIVE_DAYS, USER_STATED_MULT, CANDIDATE_EXPIRE_DAYS = 30, 90, 2, 45
MATCH = 0.6
BRIEF_HEAD = ('<learned-preferences source="honey-rsi" advisory="true">\n'
              "From your past sessions. Apply when WHEN matches; the user's current instruction always wins.\n")
BRIEF_TAIL = "</learned-preferences>"


def ledger_file() -> str:
    return paths.path("ledger.jsonl")


def load() -> list:
    return store.read_jsonl(ledger_file())


def save(rows: list) -> None:
    store.write_jsonl(ledger_file(), sorted(rows, key=lambda b: b.get("id", "")))


def _days(ts: str) -> float:
    try:
        return (time.time() - calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))) / 86400
    except Exception:
        return 0.0


def score(b: dict) -> float:
    last = b.get("last_evidence") or b.get("created") or text.now()
    return (3 * (b.get("source") == "user-stated") + 2 * b.get("severity", 1) + min(len(b.get("sessions", [])), 5)
            + b.get("helpful", 0) - 2 * b.get("harmful", 0) - _days(last) / 15)


def scope_for(note_scope: str, agent: str, project: str) -> str:
    if note_scope == "project":
        return f"project:{project}"
    if note_scope in ("claude", "codex"):
        return f"agent:{note_scope}"
    if note_scope.startswith("skill:"):
        return note_scope
    return "global"


def split_when_then(s: str):
    m = re.match(r"^(?:when|if|before|after|whenever)\s+(.{3,120}?),\s*(?:then\s+)?(.{3,200})$", s.strip(), re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "always", s.strip()


def new_bullet(when, then, scope, kind, source, severity, evidence, status) -> dict:
    ts = text.now()
    return {"id": text.short_id("b", when + then), "when": when[:120], "then": then[:200], "why": "", "kind": kind,
            "scope": scope, "applicability": "", "keys": text.keywords(when + " " + then, 6), "severity": severity,
            "source": source, "evidence": evidence, "sessions": sorted({e["sid"] for e in evidence if e.get("sid")}),
            "projects": [], "helpful": 0, "harmful": 0, "violations": 0, "relevant": 0, "injected": 0,
            "status": status, "supersedes": [], "superseded_by": None, "prev": [], "orig": None,
            "created": ts, "updated": ts, "activated": ts if status == "active" else None, "last_evidence": ts, "last_relevant": None}


def never_hit(when: str, then: str) -> bool:
    for n in store.read_jsonl(paths.path("never.jsonl")):
        if text.jaccard(n.get("text", ""), f"{when} {then}") >= MATCH:
            return True
    return False


NOTE_LOCK_WAIT = 1.5  # seconds; the prompt hook times out at 5 s, background jobs can hold the lock for minutes


def _note_record(note_text: str, note_scope: str, agent: str, sid: str, project: str):
    """Validated note fields, or None if rejected. Raises nothing."""
    clean = text.redact(note_text, 400)
    if text.injection_hit(clean):
        return None
    when, then = split_when_then(clean)
    return {"clean": clean, "when": when, "then": then, "scope": scope_for(note_scope, agent, project), "project": project,
            "ev": {"sid": f"{agent}-{sid}", "ts": text.now(), "q": clean[:300], "quarantined": False}}


def _save_note(rows: list, rec: dict):
    """Merge or append one note record into ledger rows (caller holds store.lock). Returns (bullet, merged)."""
    clean, when, then, scope, ev = rec["clean"], rec["when"], rec["then"], rec["scope"], rec["ev"]
    for b in rows:
        if b.get("scope") == scope and b.get("status") in ("active", "candidate", "proposed", "stale") \
                and text.jaccard(b["when"] + " " + b["then"], when + " " + then) >= MATCH:
            b["prev"] = (b.get("prev") or [])[-2:] + [{"when": b["when"], "then": b["then"]}]
            b.update(when=when[:120], then=then[:200], source="user-stated", orig=clean, status="active",
                     updated=text.now(), last_evidence=text.now(), activated=b.get("activated") or text.now())
            b["evidence"] = (b.get("evidence") or [])[-9:] + [ev]
            b["sessions"] = sorted(set(b.get("sessions", [])) | {ev["sid"]})
            return b, True
    b = new_bullet(when, then, scope, "preference", "user-stated", 2, [ev], "active")
    b["id"] = rec.get("id") or b["id"]  # keep the id already reported for a queued note
    b["orig"] = clean
    if rec.get("project"):
        b["projects"] = [rec["project"]]
    rows.append(b)
    return b, False


def drain_pending_notes() -> int:
    """Save notes queued while the lock was busy. Caller holds store.lock. Returns how many were saved."""
    queue = paths.path("pending-notes.jsonl")
    with store.session_lock(queue):  # appenders wait; delete only after the ledger is saved
        if not os.path.exists(queue):
            return 0
        records = store.read_jsonl(queue)
        if records:
            rows = load()
            for rec in records:
                _save_note(rows, rec)
            save(rows)
        os.remove(queue)
    return len(records)


def add_note(note_text: str, note_scope: str, agent: str, sid: str, project: str):
    """User-stated `remember:` note → active bullet. Returns (bullet, merged?), (bullet, "queued") or (None, reason)."""
    rec = _note_record(note_text, note_scope, agent, sid, project)
    if rec is None:
        return None, "rejected: looks like an instruction injection"
    with store.lock(timeout=NOTE_LOCK_WAIT) as got:
        if got:
            drain_pending_notes()
            rows = load()
            b, merged = _save_note(rows, rec)
            save(rows)
            return b, merged
    # A background job holds the lock; queue instead of losing the note when the hook times out.
    b = new_bullet(rec["when"], rec["then"], rec["scope"], "preference", "user-stated", 2, [rec["ev"]], "active")
    rec["id"] = b["id"]
    queue = paths.path("pending-notes.jsonl")
    with store.session_lock(queue):
        store.append_jsonl(queue, rec)
    return b, "queued"


def forget(bid: str) -> bool:
    with store.lock():
        rows = load()
        for b in rows:
            if b["id"] == bid:
                b["status"], b["updated"] = "archived", text.now()
                save(rows)
                store.append_jsonl(paths.path("never.jsonl"), {"ts": text.now(), "id": bid, "text": f"{b['when']} {b['then']}"})
                return True
    return False


def get(bid: str):
    return next((b for b in load() if b.get("id") == bid), None)


def line(b: dict) -> str:
    when = b["when"]
    return (f"- {b['then']} [{b['id']}]" if when == "always" else f"- WHEN {when} → {b['then']} [{b['id']}]")


def brief(agent: str, project: str) -> tuple:
    """(text, [ids]) for this agent+project, most important first, ≤ BRIEF_MAX chars. Empty when nothing learned."""
    active = [b for b in load() if b.get("status") == "active"]
    scopes = (("global", "user"), (f"project:{project}",), (f"agent:{agent}",))
    caps = (MAX_ACTIVE_GLOBAL, 5, 5)
    body, ids, size = [], [], len(BRIEF_HEAD) + len(BRIEF_TAIL)
    for group, cap in zip(scopes, caps):
        picked = sorted((b for b in active if b.get("scope") in group), key=score, reverse=True)[:cap]
        for b in picked:
            ln = line(b) + "\n"
            if size + len(ln) > BRIEF_MAX:
                continue  # overflow becomes keyword reminders (Phase 2)
            body.append(ln)
            ids.append(b["id"])
            size += len(ln)
    if not body:
        return "", []
    return BRIEF_HEAD + "".join(body) + BRIEF_TAIL, ids


def skill_notes(skill: str) -> str:
    name = skill.split(":")[-1]
    notes = sorted((b for b in load() if b.get("status") == "active" and b.get("scope") in (f"skill:{name}", f"skill:{skill}")),
                   key=score, reverse=True)[:MAX_SKILL_LINES]
    if not notes:
        return ""
    return f"honey RSI notes for `{name}` (learned from past sessions; the user's instruction wins):\n" + "\n".join(line(b) for b in notes)


def write_brief_md() -> None:
    txt, _ = brief("claude", "")
    store.write_text(paths.path("brief.md"), (txt or "(no active learnings yet)") + "\n")


# ---- reflection merge, promotion, decay (Phase 2) --------------------------------------------------------------

def _scope(raw: str, project: str, agent: str) -> str:
    raw = (raw or "global").strip().lower()
    if raw.startswith("project"):
        return f"project:{project}" if project else "global"
    if raw in ("claude", "codex", "agent:claude", "agent:codex"):
        return "agent:" + raw.split(":")[-1]
    if raw.startswith("skill:") or raw in ("user", "global"):
        return raw
    return "global"


def _evidence(ids_used, ids_map, key, quarantined):
    ev = []
    for i in ids_used or []:
        r = ids_map.get(i)
        if r:
            quote = r.get("q") or r.get("reason") or r.get("name") or ""
            # An external tool in the session does not taint the user's own
            # correction. Tool output and unknown provenance stay quarantined.
            direct_user = (r.get("e") == "prompt" and r.get("origin") == "user"
                           and not r.get("xc") and not text.injection_hit(quote))
            item = {"sid": key, "ts": r.get("ts", text.now()), "ev": i, "q": text.redact(r.get("q") or r.get("reason") or r.get("name") or "", 300),
                       "quarantined": not direct_user, "sig": r.get("sig", []),
                       "event": r.get("e"), "origin": r.get("origin")}
            for field in ("task", "feedback_kind", "attribution", "attributed_skill"):
                if field in r:
                    item[field] = r[field]
            ev.append(item)
    return ev


def skill_evidence_matches(evidence: dict, skill: str, outcome: str = "failed") -> bool:
    """Reflection cannot upgrade uncertain observations into confident skill feedback."""
    if (evidence.get("event") != "prompt" or evidence.get("origin") != "user"
            or evidence.get("quarantined") is not False or text.injection_hit(evidence.get("q", ""))):
        return False
    category = "accept" if outcome == "worked" else "behavior"
    name = (skill or "").split(":")[-1]
    if "attribution" in evidence or "feedback_kind" in evidence:
        return (evidence.get("attribution") in ("explicit", "single-task")
                and evidence.get("feedback_kind") == category
                and (evidence.get("attributed_skill") or "").split(":")[-1] == name)
    # Old logs lack task contracts. Only a direct, named assessment is sufficient.
    quote = evidence.get("q", "")
    return signals.mentions_skill(quote, name) and signals.feedback_kind(quote) == category


def revalidate_evidence(evidence: dict, events: dict | None = None) -> bool:
    """Recover legacy provenance only from the exact original user event."""
    quote = evidence.get("q", "")
    if not quote or text.injection_hit(quote):
        return False
    if evidence.get("event") or evidence.get("origin"):
        return (evidence.get("event") == "prompt" and evidence.get("origin") == "user"
                and evidence.get("quarantined") is False)
    key = evidence.get("sid", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key) or not evidence.get("ts"):
        return False
    from honey.rsi import digest
    cache = events if events is not None else {}
    if key not in cache:
        cache[key] = digest.load_events(key)
    for row in cache[key]:
        if (row.get("e") == "prompt" and row.get("origin") == "user" and not row.get("xc")
                and row.get("ts") == evidence["ts"] and not text.injection_hit(row.get("q", ""))
                and text.redact(row.get("q", ""), 300) == quote):
            evidence.update(event="prompt", origin="user", quarantined=False)
            return True
    return False


def _bump_scorecard(skill: str, field: str, agent: str) -> None:
    fp = paths.path("scorecards.json")
    sc = store.read_json(fp, {}) or {}
    name = skill.split(":")[-1]
    c = sc.setdefault(name, {"agents": {}, "pos": 0, "neg": 0, "cont": 0, "neutral": 0, "misfire": 0, "missed": 0,
                             "worked": 0, "failed": 0, "inv_user": 0, "inv_model": 0, "last_used": None})
    c[field] = c.get(field, 0) + 1
    c["agents"][agent] = c["agents"].get(agent, 0) + (1 if field.startswith("inv_") else 0)
    if field.startswith("inv_"):
        c["last_used"] = text.now()
    store.write_json(fp, sc, sort=True)


def observe_only(key: str, rows: list) -> None:
    """Deterministic bookkeeping for every session, reflected or not: skill scorecards and weekly counts."""
    agent = key.split("-", 1)[0]
    for r in rows:
        if r.get("e") == "skill":
            _bump_scorecard(r.get("name", ""), "inv_user" if r.get("by") == "user" else "inv_model", agent)
        elif r.get("e") == "skill_outcome":
            outcome = r.get("outcome", "neutral")
            if outcome not in ("pos", "neg", "neutral", "cont", "uncertain"):
                continue
            if outcome in ("pos", "neg") and (r.get("attribution") not in ("explicit", "single-task")
                    or r.get("feedback_kind") != ("accept" if outcome == "pos" else "behavior")):
                outcome = "uncertain"
            _bump_scorecard(r.get("name", ""), outcome, agent)
    prof = store.read_json(paths.path("profile.json"), {}) or {}
    week = time.strftime("%G-W%V", time.gmtime())
    w = prof.setdefault("weekly", {}).setdefault(f"{week}:{agent}", {"sessions": 0, "user_turns": 0, "correction": 0, "still": 0,
                                                                       "frustration": 0, "accept": 0, "continue": 0, "false_done": 0,
                                                                       "unneeded_q": 0, "tool_fail": 0, "compact": 0, "notes": 0})
    w["sessions"] += 1
    holdout = any(r.get("e") == "start" and r.get("holdout") for r in rows)
    last_stop = None
    for r in rows:
        e = r.get("e")
        if e == "prompt" and r.get("origin") == "user":
            w["user_turns"] += 1
            sig = set(r.get("sig", []))
            for s in ("correction", "still", "frustration", "accept", "continue"):
                w[s] += s in sig
            if holdout:
                w["holdout_turns"] = w.get("holdout_turns", 0) + 1
                w["holdout_correction"] = w.get("holdout_correction", 0) + ("correction" in sig)
            if (last_stop and last_stop.get("done") and sig & {"correction", "still", "error_paste"}
                    and r.get("feedback_kind", "behavior") == "behavior"):
                w["false_done"] += 1
            if last_stop and last_stop.get("ends_q") and "continue" in sig:
                w["unneeded_q"] += 1
        elif e == "stop":
            last_stop = r
        elif e == "tool_fail":
            w["tool_fail"] += 1
        elif e == "compact":
            w["compact"] += 1
        elif e == "note":
            w["notes"] += 1
        elif e == "tool":
            for field, value in (r.get("metrics") or {}).items():
                if field in metrics.FIELDS and type(value) in (int, float) and value >= 0:
                    name = "eff_" + field
                    w[name] = w.get(name, 0) + value
    if holdout:
        w["holdout_sessions"] = w.get("holdout_sessions", 0) + 1
    store.write_json(paths.path("profile.json"), prof, sort=True)


def apply_reflection(result: dict, key: str, ids_map: dict, meta: dict) -> dict:
    """Validate the reflector's JSON and merge it as itemized ops. The LLM never edits the ledger directly."""
    from honey.rsi import review
    agent, project, quar = meta.get("agent", "claude"), meta.get("project", ""), meta.get("quarantined", False)
    stats = {"candidates": 0, "dropped": 0, "labels": 0}
    rows = load()
    by_id = {b["id"]: b for b in rows}
    for c in (result or {}).get("candidates", [])[:5]:
        ev = _evidence(c.get("evidence"), ids_map, key, quar)
        when, then = text.redact(c.get("when", ""), 120), text.redact(c.get("then", ""), 200)
        if (not ev or not (c.get("non_obvious") and c.get("durable") and c.get("material")) or not c.get("counterfactual")
                or c.get("op") == "noop" or len(when) < 3 or len(then) < 3 or text.injection_hit(when + " " + then) or never_hit(when, then)):
            stats["dropped"] += 1
            continue
        scope = _scope(c.get("scope"), project, agent)
        target = by_id.get(c.get("target_id") or "")
        if not target:
            target = next((b for b in rows if b.get("scope") == scope and b.get("status") not in ("archived", "superseded", "rejected")
                           and text.jaccard(b["when"] + " " + b["then"], when + " " + then) >= MATCH), None)
        sev = max(1, min(3, int(c.get("severity", 1))))
        if target and c.get("op") != "supersede":
            if key not in target.get("sessions", []):
                target["sessions"] = sorted(set(target.get("sessions", [])) | {key})
            target["evidence"] = (target.get("evidence") or [])[-8:] + ev[:2]
            target["severity"] = max(target.get("severity", 1), sev)
            target["last_evidence"] = target["updated"] = text.now()
            if target.get("status") == "stale":
                target["status"] = "active" if target.get("helpful", 0) >= 1 else "candidate"
            if project and project not in target.get("projects", []):
                target["projects"] = target.get("projects", []) + [project]
        else:
            b = new_bullet(when, then, scope, c.get("kind", "pattern"), "observed", sev, ev, "candidate")
            b["applicability"] = text.redact(c.get("applicability", ""), 180)
            b["why"] = text.redact(c.get("counterfactual", ""), 180)
            b["projects"] = [project] if project else []
            if target:  # supersede
                target["status"], target["superseded_by"] = "superseded", b["id"]
                b["supersedes"] = [target["id"]]
                for k in ("helpful", "harmful", "relevant"):
                    b[k] = target.get(k, 0)
                b["sessions"] = sorted(set(b["sessions"]) | set(target.get("sessions", [])))
            rows.append(b)
            by_id[b["id"]] = b
        stats["candidates"] += 1
    for lab in (result or {}).get("bullet_labels", [])[:25]:
        b = by_id.get(lab.get("id", ""))
        if not b or not _evidence(lab.get("evidence"), ids_map, key, quar):
            continue
        field = {"helpful": "helpful", "violated": "violations", "harmful": "harmful"}.get(lab.get("label"))
        if field:
            b[field] = b.get(field, 0) + 1
            b["relevant"] = b.get("relevant", 0) + 1
            b["last_relevant"] = text.now()
            stats["labels"] += 1
    save(rows)
    for f in (result or {}).get("skill_feedback", [])[:8]:
        ev = [ev for ev in _evidence(f.get("evidence"), ids_map, key, quar)
              if skill_evidence_matches(ev, f.get("skill", ""), f.get("outcome"))]
        if ev and f.get("outcome") in ("worked", "failed", "misfired", "missed"):
            _bump_scorecard(f.get("skill", ""), {"misfired": "misfire"}.get(f["outcome"], f["outcome"]), agent)
            store.append_jsonl(paths.path("skill-feedback.jsonl"), {
                "ts": text.now(), "sid": key, "skill": text.redact(f.get("skill", "").split(":")[-1], 60),
                "outcome": f["outcome"], "issue": text.redact(f.get("issue", ""), 180), "evidence": ev})
    for m in (result or {}).get("toolkit_misfires", [])[:4]:
        ev = _evidence(m.get("evidence"), ids_map, key, quar)
        issue = text.redact(m.get("issue", ""), 180)
        if ev and issue and not text.injection_hit(issue):
            review.add(1, "toolkit_fix", f"{text.redact(m.get('component', ''), 60)}: {issue}"[:100], issue,
                       "Investigate the component; propose a minimal fix in /rsi review.", ev, "misfire:" + m.get("component", "") + issue[:60])
    prof = store.read_json(paths.path("profile.json"), {}) or {}
    for p in (result or {}).get("progress", [])[:6]:
        if _evidence(p.get("evidence"), ids_map, key, quar):
            d = prof.setdefault("domains", {}).setdefault(text.redact(p.get("domain", "misc"), 40).lower(), {"growing": 0, "repeating": 0, "new": 0, "sessions": []})
            d[p.get("direction", "new")] = d.get(p.get("direction", "new"), 0) + 1
            d["sessions"] = (d.get("sessions", []) + [key])[-20:]
            d["last"] = text.now()
    store.write_json(paths.path("profile.json"), prof, sort=True)
    return stats


def min_sessions() -> int:
    """Default 2; tightened to 3 when reviews show observed learnings are mostly rejected (precision < .3 over ≥10)."""
    cfg = store.read_json(paths.path("config.json"), {}) or {}
    base = int((cfg.get("autonomy") or {}).get("min_sessions", 2))
    gold = [g for g in store.read_jsonl(paths.path("golden.jsonl")) if g.get("source") == "observed"]
    if len(gold) >= 10 and sum(g.get("decision") in ("apply", "edit") for g in gold) / len(gold) < 0.3:
        return max(base, 3)
    return base


def maintain() -> dict:
    """Promotion, demotion, decay and budgets. Deterministic; run after every reflection batch."""
    rows, now = load(), text.now()
    need = min_sessions()
    moved = {"activated": 0, "stale": 0, "archived": 0, "demoted": 0}
    events = {}
    for b in rows:
        st = b.get("status")
        last = max(b.get("last_evidence") or "", b.get("last_relevant") or "") or b.get("created", now)
        age = _days(last)
        user = b.get("source") == "user-stated"
        if st == "candidate":
            if never_hit(b.get("when", ""), b.get("then", "")):
                b.update(status="archived", updated=now)
                moved["archived"] += 1
                continue
            for evidence in b.get("evidence", []):
                if evidence.get("quarantined"):
                    revalidate_evidence(evidence, events)
            clean = any(not e.get("quarantined") for e in b.get("evidence", [])) or user
            corr3 = b.get("severity", 1) >= 3 and any("correction" in (e.get("sig") or []) for e in b.get("evidence", []))
            if (len(b.get("sessions", [])) >= need or user or corr3) and b.get("helpful", 0) >= b.get("harmful", 0) and clean:
                b.update(status="active", activated=now, updated=now)
                moved["activated"] += 1
            elif age > CANDIDATE_EXPIRE_DAYS:
                b.update(status="archived", updated=now)
                moved["archived"] += 1
        elif st == "active":
            if b.get("harmful", 0) > b.get("helpful", 0) + 1:
                b.update(status="proposed", updated=now)
                moved["demoted"] += 1
            elif age > STALE_DAYS * (USER_STATED_MULT if user else 1):
                b.update(status="stale", updated=now)
                moved["stale"] += 1
        elif st == "stale" and age > ARCHIVE_DAYS:
            b.update(status="archived", updated=now)
            moved["archived"] += 1
    for group, cap in ((("global", "user"), MAX_ACTIVE_GLOBAL),):
        act = sorted((b for b in rows if b.get("status") == "active" and b.get("scope") in group), key=score, reverse=True)
        for b in act[cap:]:
            b.update(status="stale", updated=now)
            moved["stale"] += 1
    projects = {b["scope"] for b in rows if b.get("scope", "").startswith("project:")}
    for pscope in projects:
        act = sorted((b for b in rows if b.get("status") == "active" and b.get("scope") == pscope), key=score, reverse=True)
        for b in act[MAX_ACTIVE_PROJECT:]:
            b.update(status="stale", updated=now)
            moved["stale"] += 1
    save(rows)
    return moved
