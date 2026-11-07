"""Apply structured RSI proposals under the caller's RSI lock, without human review."""
from __future__ import annotations

import json
import os
import re
import time

from honey import paths
from honey.rsi import curate, review, store, text

MAX_PER_RUN = 20


class Deferred(ValueError):
    """Waiting for a child operation or a resource budget."""


def enabled() -> bool:
    cfg = store.read_json(paths.path("config.json"), {}) or {}
    return (cfg.get("rsi") or {}).get("autonomous") is True


def configure(value: bool) -> None:
    cfg = store.read_json(paths.path("config.json"), {}) or {}
    cfg.setdefault("rsi", {})["autonomous"] = value
    store.write_json(paths.path("config.json"), cfg)


def apply(proposal: dict) -> str:
    """Perform an effect or raise; never interpret free-form fix text as code."""
    from honey.rsi import instructions
    if proposal.get("type") in ("toolkit_fix", "skill_new") and proposal.get("child"):
        child = proposal["child"]
        if not isinstance(child, str) or paths.safe_id(child) != child:
            raise ValueError("invalid linked skill patch ID")
        saved = store.read_json(paths.path("proposals", child + ".json"))
        if not saved or saved.get("id") != child or saved.get("type") != "skill_patch":
            raise ValueError("linked skill patch is missing")
        if saved.get("status") == "applied":
            return "linked skill patch applied: " + child
        raise Deferred("waiting for linked skill patch " + child + " (" + str(saved.get("status")) + ")")
    if proposal.get("type") in ("instr_line", "instr_remove", "instr_edit"):
        return instructions.apply(proposal)
    if proposal.get("type") == "toolkit_fix":
        return repair_toolkit(proposal)
    if proposal.get("type") == "guard":
        from honey.rsi import guards
        return guards.activate(proposal.get("guard"))
    if proposal.get("type") == "skill_patch":
        from honey.rsi import patch
        return patch.accept(proposal)
    if proposal.get("type") == "upstream_port":
        child = proposal.get("child")
        if child:
            saved = store.read_json(paths.path("proposals", paths.safe_id(child) + ".json"))
            if not saved or saved.get("type") != "skill_patch":
                raise ValueError("linked skill patch is missing")
            if saved.get("status") == "applied":
                return "linked skill patch applied: " + child
            raise Deferred("waiting for linked skill patch " + child + " (" + str(saved.get("status")) + ")")
        skill = proposal.get("skill", "")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", skill) or not os.path.isfile(os.path.join(paths.REPO, "skills", skill, "SKILL.md")):
            raise ValueError("unknown upstream skill")
        from honey.rsi import patch
        result = patch.propose(skill, "upstream", proposal.get("upstream", ""))
        if not result.get("proposed"):
            raise Deferred("upstream patch not ready: " + str(result))
        proposal["child"] = result["proposed"]
        store.write_json(paths.path("proposals", proposal["id"] + ".json"), proposal)
        raise Deferred("drafted linked skill patch " + proposal["child"])
    raise ValueError("no structured handler for " + str(proposal.get("type")))


def repair_toolkit(proposal: dict) -> str:
    """Apply one known audit operation, reusing install's backup/undo records."""
    from honey import install
    repair = proposal.get("repair") or {}
    operation = repair.get("operation")
    steps = []
    env = install.Env()
    env.backups = paths.path("backups", "rsi", paths.safe_id(proposal["id"]))
    if operation == "broken_links":
        directory = repair.get("directory")
        allowed = [os.path.join(d, "skills") for d in (paths.claude_dir(), paths.agents_dir(), paths.codex_dir())]
        if directory not in allowed or not repair.get("links"):
            raise ValueError("invalid broken-link repair target")
        for name, target in repair["links"].items():
            fp = os.path.join(directory, name)
            if os.path.basename(name) != name or not os.path.islink(fp) or os.readlink(fp) != target or os.path.exists(fp):
                raise ValueError("skill link changed since audit")
            steps.append({"kind": "unlink", "path": fp, "target": target,
                          "summary": f"RSI repair: remove broken skill link {fp}"})
    elif operation in ("remove_hook", "statusline", "disable_plugins"):
        fp = os.path.join(paths.claude_dir(), "settings.json")
        raw = install._read(fp)
        settings = json.loads(raw or "null")
        if not isinstance(settings, dict):
            raise ValueError("settings.json is missing or invalid")
        undo = {}
        if operation == "remove_hook":
            name = repair.get("name")
            if name not in ("mct-prompt-router.py", "session-context.py", "completion-receipt.py"):
                raise ValueError("unsupported hook repair")
            current = [[e, c] for e, c in review._hook_commands(settings) if name in c]
            if not current or current != repair.get("expected"):
                raise ValueError("hook changed since audit")
            removed = []
            for event, groups in list((settings.get("hooks") or {}).items()):
                kept = []
                for group in groups:
                    hooks = group.get("hooks") or []
                    remaining = [h for h in hooks if name not in h.get("command", "")]
                    removed.extend({"event": event, "matcher": group.get("matcher"), "hook": h} for h in hooks if h not in remaining)
                    if remaining:
                        kept.append(dict(group, hooks=remaining))
                if kept:
                    settings["hooks"][event] = kept
                else:
                    del settings["hooks"][event]
            undo["removed_hooks"] = removed
        elif operation == "statusline":
            old = settings.get("statusLine")
            if old != repair.get("expected") or not isinstance(old, dict):
                raise ValueError("statusLine changed since audit")
            undo["statusLine"] = old
            launcher = os.path.join(paths.REPO, "bin", "honey")
            if "/plugins/cache/" in launcher:
                launcher = install._cli("honey")
                if not launcher or "/plugins/cache/" in launcher:
                    raise ValueError("no stable honey launcher; install the honey CLI before repairing statusLine")
            settings["statusLine"] = {"type": "command", "command": f'python3 "{launcher}" statusline'}
        else:
            plugins = repair.get("plugins") or []
            if not plugins or any(p not in install.CLAUDE_CLASH for p in plugins):
                raise ValueError("unsupported plugin repair")
            if any((settings.get("enabledPlugins") or {}).get(p) is not True for p in plugins):
                raise ValueError("plugin setting changed since audit")
            undo["plugins"] = {p: True for p in plugins}
            for p in plugins:
                settings["enabledPlugins"][p] = False
        steps.append({"kind": "edit_json", "file": fp, "old": raw,
                      "new": json.dumps(settings, indent=2, ensure_ascii=False) + "\n", "undo": undo,
                      "summary": f"RSI repair ({operation}): {fp}"})
    else:
        raise ValueError("toolkit diagnosis has no structured repair")
    records = proposal.setdefault("repair_actions", [])
    for step in steps:
        rec = install._apply(env, step)
        records.append(rec)
        store.write_json(paths.path("proposals", proposal["id"] + ".json"), proposal)
        manifest = store.read_json(env.manifest, {"actions": []}) or {"actions": []}
        manifest["actions"].append(rec)
        store.write_json(env.manifest, manifest)
    return f"repaired {operation} ({len(steps)} changes; undo records saved)"


def run() -> dict:
    """Drain a bounded snapshot. Caller owns store.lock; do not nest it here."""
    from honey.rsi import publish
    announced = publish.run(include_patches=False)
    if not enabled():
        return {"skipped": "autonomy disabled", "publication": announced}
    out = {"applied": [], "changes": [], "blocked": [], "deferred": []}
    queue = sorted(review.pending(), key=lambda p: (p.get("last_attempt_ns", 0), p.get("created", ""), p["id"]))
    for proposal in queue[:MAX_PER_RUN]:
        try:
            validate_sources(proposal)
            if proposal.get("type") == "upstream_port" and not proposal.get("child"):
                raise Deferred("upstream patch awaits the daily improvement pass")
            if proposal.get("type") in ("instr_edit", "instr_remove"):
                from honey.rsi import instructions
                if not instructions.in_managed_block(proposal):
                    raise Deferred("edits to your own instruction lines wait for /rsi review")
            preparation = proposal.get("preparation") or {}
            if not proposal.get("child") and not proposal.get("repair") and (
                    proposal.get("type") == "skill_new" or
                    proposal.get("type") == "toolkit_fix" and preparation):
                if preparation.get("blocked"):
                    raise ValueError(preparation["blocked"])
                reason = preparation.get("skipped") or preparation.get("error") or "awaiting daily skill preparation"
                raise Deferred(str(reason))
            result = apply(proposal)
            saved = review.decide(proposal["id"], "apply", result)
            saved.update(decision_source="automatic", last_error=None, last_attempt=text.now(),
                         last_attempt_ns=time.time_ns(), attempt_state="applied")
            store.write_json(paths.path("proposals", saved["id"] + ".json"), saved)
            out["applied"].append(saved["id"])
            out["changes"].append({"id": saved["id"], "type": saved.get("type", ""),
                                   "title": saved.get("title", ""), "result": result})
        except (Exception, SystemExit) as exc:
            saved = store.read_json(paths.path("proposals", proposal["id"] + ".json"), proposal)
            if not isinstance(saved, dict) or saved.get("id") != proposal["id"]:
                saved = proposal
            state = "deferred" if isinstance(exc, Deferred) else "blocked"
            saved.update(last_error=text.redact(str(exc), 400), last_attempt=text.now(),
                         last_attempt_ns=time.time_ns(), attempt_state=state)
            store.write_json(paths.path("proposals", saved["id"] + ".json"), saved)
            out[state].append({"id": saved["id"], "reason": saved["last_error"]})
    out['publication'] = publish.run(suggestion_result=announced.get('suggestions'))
    curate.write_brief_md()
    store.write_json(paths.path("state", "autonomy-last.json"), dict(out, ts=text.now()))
    return out


def validate_sources(proposal: dict) -> None:
    """Do not resurrect a rule forgotten or superseded since proposal creation."""
    remote_block = review.remote_block_reason(proposal)
    if remote_block:
        raise Deferred(remote_block)
    sources = list(proposal.get("bullet_ids") or [])
    if proposal.get("type") in ("instr_edit", "instr_remove") and not sources:
        raise ValueError("instruction audit has no source learning IDs; re-audit or apply manually")
    source = (proposal.get("edit") or {}).get("bullet")
    if source:
        sources.append(source)
    if proposal.get("type") == "guard":
        from honey.rsi import guards
        guard = next((g for g in guards.load() if g.get("id") == proposal.get("guard")), {})
        if guard.get("bullet"):
            sources.append(guard["bullet"])
    for ident in sources:
        b = curate.get(ident)
        if not b or b.get("status") != "active" or curate.never_hit(b.get("when", ""), b.get("then", "")):
            raise ValueError("source learning is no longer eligible: " + str(ident))
