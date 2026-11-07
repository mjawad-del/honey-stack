"""`honey rsi …` commands. The `rsi` skill runs these and renders the output."""
from __future__ import annotations

import json
import os
import time

from honey import paths
from honey.rsi import curate, review, store, text


def _print(obj, as_json):
    print(json.dumps(obj, indent=1, ensure_ascii=False) if as_json else obj)


def status(as_json=False):
    from honey.rsi import autonomy, publish, reflect
    rows = curate.load()
    by = {}
    for b in rows:
        by[b.get("status")] = by.get(b.get("status"), 0) + 1
    txt, ids = curate.brief("claude", "")
    costs = store.read_jsonl(paths.path("costs.jsonl"))
    month_rows = [c for c in costs if c.get("ts", "").startswith(time.strftime("%Y-%m"))]
    month = sum(c.get("usd") or 0 for c in month_rows)
    unknown_costs = sum(c.get("usd") is None for c in month_rows)
    ev_dir = paths.path("events")
    n_sessions = sum(len(fs) for _, _, fs in os.walk(ev_dir)) if os.path.isdir(ev_dir) else 0
    suggestion_issues = [dict(id=p['id'], status=p.get('status'), **{
        key: p['github_issue'][key] for key in
        ('url', 'remote_state', 'state_reason', 'closure', 'checked_at', 'reconciliation_error')
        if key in p['github_issue']}) for p in review.all_proposals() if p.get('github_issue')]
    out = {"suggestion_issues": suggestion_issues, "home": paths.home(), "bullets": by, "pending_review": len(review.pending()), "brief_chars": len(txt),
           "brief_ids": ids, "sessions_observed": n_sessions, "usd_this_month": round(month, 4),
           "autonomous": autonomy.enabled(),
           "publication": publish.config(),
           "recent_publication": store.read_json(paths.path("state", "publication-last.json"), {}) or {},
           "budget_caps_enabled": reflect.dollar_caps_enabled(),
           "unknown_cost_calls_this_month": unknown_costs,
           "monthly_cost_complete": unknown_costs == 0,
           "recent_autonomy": store.read_json(paths.path("state", "autonomy-last.json"), {}) or {},
           "recent_improvement": store.read_json(paths.path("state", "improvement-last.json"), {}) or {}}
    if as_json:
        return _print(out, True)
    print(f"honey RSI · {out['home']}")
    print("  autonomy: " + ("enabled" if out["autonomous"] else "disabled (manual review)"))
    publishing = out['publication']
    kinds = ' (issues + PRs)' if publishing.get('suggestions') else ' (PRs)'
    print('  publication: ' + (publishing.get('repository', '') + kinds if publishing.get('enabled') else 'disabled'))
    for item in out['recent_publication'].get('published', []):
        print(f"    {item['id']}: {item['url']} head {item['sha']}")
    for item in out['recent_publication'].get('blocked', []):
        print(f"    {item['id']}: publication blocked: {item['reason']}")
    for item in out['recent_publication'].get('suggestions', {}).get('published', []):
        print(f"    {item['id']}: suggestion issue {item['url']}")
    for item in out['recent_publication'].get('suggestions', {}).get('blocked', []):
        print(f"    {item['id']}: issue publication blocked: {item['reason']}")
    for category, items in out['recent_publication'].get('delivery', {}).items():
        for item in items:
            print(f"    delivery {category}: {item if isinstance(item, str) else item.get('id')}")
    print("  dollar caps: " + ("enabled" if out["budget_caps_enabled"] else "disabled; rate limits still apply"))
    print(f"  learnings: {by or 'none yet'}")
    print(f"  pending items: {out['pending_review']}   brief: {out['brief_chars']} chars ({len(ids)} lines)")
    usage = (f"known usage this month: ${out['usd_this_month']}; {unknown_costs} calls with unknown cost"
             if unknown_costs else f"spend this month: ${out['usd_this_month']}")
    print(f"  sessions observed: {n_sessions}   {usage}")
    if out["recent_improvement"]:
        last = out["recent_improvement"]
        if last.get('last_checked_at'):
            print(f"  last improvement check: {last['last_checked_at']} {last.get('last_check_result', {})}")
        else:
            print(f"  last improvement: {last.get('ts', '')} {last.get('id', '')} {last.get('result', {})}")
    recent = out["recent_autonomy"]
    if recent:
        print(f"  last automatic pass: {len(recent.get('applied', []))} applied, "
              f"{len(recent.get('blocked', []))} blocked, {len(recent.get('deferred', []))} deferred")
        for change in recent.get("changes", []):
            print(f"    {change['id']} ({change.get('type', '')}): {change.get('title', '')}")
            print(f"      {change.get('result', '')}")


def why(bid, as_json=False):
    b = curate.get(bid)
    if not b:
        raise SystemExit(f"no learning {bid}")
    if as_json:
        return _print(b, True)
    print(curate.line(b)[2:])
    print(f"  status={b['status']} scope={b['scope']} source={b['source']} severity={b.get('severity')}")
    print(f"  helpful={b.get('helpful',0)} harmful={b.get('harmful',0)} violations={b.get('violations',0)} sessions={len(b.get('sessions',[]))}")
    for e in b.get("evidence", [])[-5:]:
        print(f"  evidence {e.get('ts','')} {e.get('sid','')}: {e.get('q','')}")
    for p in b.get("prev", []):
        print(f"  was: WHEN {p['when']} → {p['then']}")


def list_review(as_json=False):
    from honey.rsi import autonomy
    review.audit()
    items = review.pending()
    proposed = [b for b in curate.load() if b.get("status") == "proposed"]
    if as_json:
        return _print({"proposals": items, "proposed_learnings": proposed, "autonomous": autonomy.enabled(),
                       "recent_autonomy": store.read_json(paths.path("state", "autonomy-last.json"), {}) or {},
                       "recent_improvement": store.read_json(paths.path("state", "improvement-last.json"), {}) or {},
                       "recent_changes": sorted((p for p in review.all_proposals() if p.get("decision_source") == "automatic"),
                                                key=lambda p: p.get("decided", ""), reverse=True)[:10]}, True)
    if autonomy.enabled():
        print("Autonomy enabled; pending items are retried automatically when eligible.")
    if not items and not proposed:
        print("Nothing to review.")
    for p in items:
        print(f"[{p['id']}] tier {p.get('tier', '?')} {p.get('type', 'invalid')}: {p.get('title', '')}\n"
              f"    why: {p.get('rationale', '')}\n    fix: {p.get('fix', '')}")
        if p.get("last_error"):
            print(f"    {p.get('attempt_state', 'blocked')}: {p['last_error']}")
        issue = p.get('github_issue') or {}
        if issue:
            print(f"    GitHub issue: {issue.get('url') or issue.get('error') or issue.get('state')}")
    for b in proposed:
        print(f"[{b['id']}] learning: {curate.line(b)[2:]}  (sessions={len(b.get('sessions', []))})")


def _golden(b, action):
    store.append_jsonl(paths.path("golden.jsonl"), {"ts": text.now(), "id": b["id"], "source": b.get("source"), "decision": action,
                                                   "when": b["when"], "then": b["then"], "sessions": b.get("sessions", [])[:5]})


def decide(item_id, action, note=""):
    if item_id.startswith("b-"):
        with store.lock():
            rows = curate.load()
            b = next((r for r in rows if r["id"] == item_id), None)
            if not b:
                raise SystemExit(f"no learning {item_id}")
            if action in ("apply", "edit"):
                if action == "edit" and note:
                    b["when"], b["then"] = curate.split_when_then(text.redact(note, 400))
                    b["source"] = "user-stated"
                b["status"], b["activated"] = "active", b.get("activated") or text.now()
            elif action == "skip":
                b["status"] = "candidate"
            elif action == "never":
                b["status"] = "rejected"
                store.append_jsonl(paths.path("never.jsonl"), {"id": item_id, "text": f"{b['when']} {b['then']}"})
            curate.save(rows)
            _golden(b, action)
        print(f"{item_id} → {b['status']}")
        store.git_commit(f"rsi: {action} {item_id}")
        return
    with store.lock():
        return _decide_proposal(item_id, action, note)


def _decide_proposal(item_id, action, note):
    p = store.read_json(paths.path("proposals", paths.safe_id(item_id) + ".json"))
    if not p:
        raise SystemExit(f"no proposal {item_id}")
    kind, msg = p.get("type", ""), ""
    if action in ("apply", "edit"):
        from honey.rsi import autonomy
        if p.get("status") == "applied":
            print(f"{item_id} already applied")
            return
        if kind.startswith("instr_") and action == "edit" and note and p.get("edit"):
            p["edit"]["line" if p["edit"]["action"] == "add" else "replacement"] = text.redact(note, 400)
        try:
            msg = autonomy.apply(p)
        except autonomy.Deferred as exc:
            print(f"{item_id} pending: {exc}")
            return
    elif kind == "skill_patch":
        from honey.rsi import patch
        patch.discard(p)
    elif kind == "guard":
        from honey.rsi import guards
        guards.set_status(p["guard"], "rejected")
    p = review.decide(item_id, action, note)
    print(f"{item_id} → {p['status']}" + (f". {msg}" if msg else ""))
    if action in ('apply', 'edit') and kind == 'skill_patch':
        from honey.rsi import publish
        _print(publish.run(), False)
    store.git_commit(f"rsi: {action} {item_id}")


def session_end(key):
    """Detached after SessionEnd. Phase 1: refresh brief.md and commit; Phase 2 adds reflection."""
    from honey.rsi import reflect
    state = store.read_json(paths.path("state", "sessions", paths.safe_id(key) + ".json"), {}) or {}
    if state.get("off") or state.get("rsi_off"):
        return
    reflect.run_pending()


def main(args) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="honey rsi")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("autonomy")
    s.add_argument("action", choices=["on", "off", "status"], default="status", nargs="?")
    s = sub.add_parser('publish', help='Publish suggestion issues and applied skill PRs')
    s.add_argument('action', choices=['on', 'off', 'status', 'run', 'automatic'], default='status', nargs='?')
    s.add_argument('--json', action='store_true')
    for name in ("status", "review", "brief", "audit", "improve"):
        s = sub.add_parser(name)
        s.add_argument("--json", action="store_true")
    for name in ("why", "forget"):
        s = sub.add_parser(name)
        s.add_argument("id")
        s.add_argument("--json", action="store_true")
    s = sub.add_parser("decide")
    s.add_argument("id")
    s.add_argument("action", choices=["apply", "edit", "skip", "never"])
    s.add_argument("--text", default="")
    s = sub.add_parser("note")
    s.add_argument("text", nargs="+")
    s.add_argument("--scope", default="global")
    s = sub.add_parser("session-end")
    s.add_argument("key")
    sub.add_parser("catchup")
    for name in ("coach", "weekly", "radar", "costs", "golden", "guards"):
        s = sub.add_parser(name)
        s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("revert")
    s.add_argument("id")
    s = sub.add_parser("bootstrap")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("reflect")
    s.add_argument("--session", required=True)
    a = ap.parse_args(args)
    if a.cmd == 'publish':
        from honey.rsi import publish
        with store.lock():
            _print(publish.run() if a.action == 'run' else publish.configure(a.action), a.json)
    elif a.cmd == "autonomy":
        from honey.rsi import autonomy
        with store.lock():
            if a.action != "status":
                autonomy.configure(a.action == "on")
            print("honey RSI autonomy " + ("enabled" if autonomy.enabled() else "disabled"))
    elif a.cmd in (None, "status"):
        status(getattr(a, "json", False))
    elif a.cmd == "review":
        list_review(a.json)
    elif a.cmd == "improve":
        from honey.rsi import autonomy, improve
        with store.lock():
            curated = curate.maintain()
            result = improve.run()
            applied = autonomy.run()
            curate.write_brief_md()
            store.git_commit("rsi: improve")
        _print({"curation": curated, "improvement": result, "application": applied}, a.json)
    elif a.cmd == "brief":
        txt, _ = curate.brief("claude", "")
        print(txt or "(no active learnings yet)")
    elif a.cmd == "audit":
        found = review.audit()
        if a.json:
            _print([{"id": p["id"], "title": p["title"], "status": p["status"]} for p in found], True)
        elif not found:
            print("No toolkit misfires found.")
        for p in [] if a.json else found:
            print(f"[{p['id']}] {p['status']}: {p['title']}")
    elif a.cmd == "why":
        why(a.id, a.json)
    elif a.cmd == "forget":
        print(f"{a.id} archived and added to never-list" if curate.forget(a.id) else f"no learning {a.id}")
        store.git_commit(f"rsi: forget {a.id}")
    elif a.cmd == "decide":
        decide(a.id, a.action, a.text)
    elif a.cmd == "note":
        b, merged = curate.add_note(" ".join(a.text), a.scope, "cli", "cli", paths.project_slug(os.getcwd()))
        print(f"{'queued' if merged == 'queued' else 'saved'} {b['id']}: {curate.line(b)[2:]}" if b else f"not saved: {merged}")
        if b:
            store.git_commit(f"rsi: note {b['id']}")
    elif a.cmd == "session-end":
        session_end(a.key)
    elif a.cmd == "bootstrap":
        from honey.rsi import bootstrap
        return bootstrap.run(a.dry_run)
    elif a.cmd == "coach":
        from honey.rsi import coach
        print(coach.report())
    elif a.cmd == "weekly":
        from honey.rsi import weekly
        print(json.dumps(weekly.run(), indent=1, default=str))
    elif a.cmd == "radar":
        from honey.rsi import radar
        for p in radar.run(a.dry_run) or []:
            print(f"[{p.get('id', '')}] {p.get('title', p.get('file', ''))}")
    elif a.cmd == "costs":
        rows = store.read_jsonl(paths.path("costs.jsonl"))
        by = {}
        for c in rows:
            k = (c.get("ts", "")[:7], c.get("job"))
            bucket = by.setdefault(k, {"usd": 0, "unknown": 0})
            bucket["usd"] += c.get("usd") or 0
            bucket["unknown"] += c.get("usd") is None
        for (m, j), v in sorted(by.items()):
            suffix = f" + {v['unknown']} calls with unknown cost" if v['unknown'] else ""
            print(f"{m} {j:10} ${v['usd']:.4f}{suffix}")
        unknown = sum(v['unknown'] for v in by.values())
        label = 'known subtotal' if unknown else 'total'
        suffix = f"; {unknown} calls with unknown cost" if unknown else ""
        print(f"{label} ${sum(v['usd'] for v in by.values()):.4f} over {len(rows)} calls{suffix}")
    elif a.cmd == "guards":
        from honey.rsi import guards
        for g in guards.load():
            print(f"[{g['id']}] {g['status']:9} {g['action']:5} /{g['regex']}/ fired={g.get('fired', 0)}  {g['message']}")
    elif a.cmd == "golden":
        from honey.rsi import golden
        print(golden.run(a.dry_run))
    elif a.cmd == "revert":
        from honey.rsi import instructions
        with store.lock():
            print(instructions.revert(a.id))
    elif a.cmd == "catchup":
        from honey.rsi import reflect
        reflect.run_pending()
    elif a.cmd == "reflect":
        from honey.rsi import autonomy, digest, improve, reflect
        with store.lock():
            print(reflect.reflect_session(a.session, digest.load_events(a.session)))
            curate.maintain()
            improve.run()
            autonomy.run()
            curate.write_brief_md()
            store.git_commit(f"rsi: reflect {a.session}")
    return 0
