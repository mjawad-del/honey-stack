"""honey doctor — read-only health check for both agents and RSI. Exit 1 if any check fails."""
from __future__ import annotations

import glob
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from honey import paths
from honey.install import CLAUDE_CLASH, CODEX_CLASH, MCT_HOOKS, PLUGIN
from honey.rsi import review, store

BUDGET = 3500


class Report:
    def __init__(self):
        self.fails = 0

    def ok(self, cond, good, bad, warn=False):
        mark = "ok  " if cond else ("warn" if warn else "FAIL")
        if not cond and not warn:
            self.fails += 1
        print(f"  {mark} {good if cond else bad}")


def _tree_hash(root: str, parts=("hooks", "honey", "skills", "bin")) -> str:
    h = hashlib.sha1()
    for part in parts:
        for fp in sorted(glob.glob(os.path.join(root, part, "**", "*"), recursive=True)):
            if os.path.isfile(fp) and "__pycache__" not in fp:
                h.update(os.path.relpath(fp, root).encode())
                with open(fp, "rb") as f:
                    h.update(f.read())
    return h.hexdigest()[:12]


def agent_browser() -> str:
    """Version of agent-browser on PATH, or "" when it is absent or does not answer."""
    exe = shutil.which("agent-browser")
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout.strip().split() or [""])[-1] if r.returncode == 0 else ""


def skills_budget() -> tuple:
    total, users = 0, 0
    for fp in glob.glob(os.path.join(paths.REPO, "skills", "*", "SKILL.md")):
        with open(fp, encoding="utf-8") as f:
            fm = f.read().split("---")[1]
        if "disable-model-invocation: true" in fm:
            users += 1
            continue
        m = re.search(r"^description:\s*(.*)$", fm, re.M)
        total += len(m.group(1).strip().strip('"')) if m else 0
    return total, users


def smoke() -> tuple:
    with tempfile.TemporaryDirectory() as h:
        t0 = time.time()
        r = subprocess.run([sys.executable, os.path.join(paths.REPO, "bin", "honey"), "hook", "--agent", "claude"],
                           input=json.dumps({"hook_event_name": "SessionStart", "source": "startup", "session_id": "doctor", "cwd": h}),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=dict(os.environ, HONEY_HOME=h), timeout=10)
        ms = int((time.time() - t0) * 1000)
    try:
        ok = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"].startswith("honey-stack active")
    except Exception:
        ok = False
    return ok, ms


def main(argv: list) -> int:
    if argv:
        from honey import codex_hooks, health
        parser = argparse.ArgumentParser(description='Inspect delivery and coverage for one actual session.')
        parser.add_argument('--agent', choices=('claude', 'codex'), default='codex')
        parser.add_argument('--session', help='Session ID; defaults to the selected agent session environment variable.')
        parser.add_argument('--json', action='store_true', help='Print machine-readable session health.')
        args = parser.parse_args(argv)
        sid = args.session or os.environ.get('CODEX_THREAD_ID' if args.agent == 'codex' else 'CLAUDE_SESSION_ID')
        result = health.report(args.agent, sid)
        if args.agent == 'codex':
            result['hook_trust'] = codex_hooks.inspect()
            result['healthy'] = result['healthy'] and result['hook_trust']['status'] == 'trusted'
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(f"honey doctor: {args.agent} session {sid or '(not supplied)'}")
            print(f"  installed={result['installed']} enabled={result['enabled']} delivery={result['delivery']} paused={result['paused']}")
            print(f"  adapter coverage={result['coverage']['status']} runtime={result['runtime_version'] or 'unobserved'}")
            if args.agent == 'codex':
                print('  hook trust=' + result['hook_trust']['status'])
            if result['coverage']['unsupported_tools']:
                print('  unsupported tools: ' + ', '.join(result['coverage']['unsupported_tools']))
            print('  Coverage describes observed payloads; policy enforcement is not established by a heartbeat.')
        return 0 if result['healthy'] else 1
    rep = Report()
    cdir, xdir = paths.claude_dir(), paths.codex_dir()
    print("honey doctor")
    print(f"repo {paths.REPO}")

    print("Claude Code")
    s = store.read_json(os.path.join(cdir, "settings.json"), {}) or {}
    en = s.get("enabledPlugins") or {}
    rep.ok(en.get(PLUGIN) is True, f"{PLUGIN} enabled", f"{PLUGIN} not enabled (run honey install)")
    for p in CLAUDE_CLASH:
        rep.ok(not en.get(p), f"{p} disabled", f"{p} still enabled: competing always-on injector")
    cmds = [h.get("command", "") for gs in (s.get("hooks") or {}).values() for g in gs or [] for h in g.get("hooks") or []]
    left = [m for m in MCT_HOOKS if any(m in c for c in cmds)]
    rep.ok(not left, "no MCT hook handlers", f"MCT handlers still registered: {left} (mct self-update drift?)")
    rep.ok("honey" in (s.get("statusLine") or {}).get("command", ""), "statusLine → honey", "statusLine not honey's", warn=True)
    broken = [n for n in glob.glob(os.path.join(cdir, "skills", "*")) if os.path.islink(n) and not os.path.exists(n)]
    rep.ok(not broken, "no dangling skill links", f"{len(broken)} dangling skill links in {cdir}/skills")
    inst = (store.read_json(os.path.join(cdir, "plugins", "installed_plugins.json"), {}) or {}).get("plugins", {}).get(PLUGIN) or []
    where = inst[0].get("installPath", "") if inst else ""
    mk = (store.read_json(os.path.join(cdir, "plugins", "known_marketplaces.json"), {}) or {}).get("honey-stack") or {}
    in_place = os.path.realpath(((mk.get("source") or {}).get("path") or "")) == os.path.realpath(paths.REPO)
    rep.ok(not inst or in_place or os.path.realpath(where) == os.path.realpath(paths.REPO) or _tree_hash(where) == _tree_hash(paths.REPO),
           "Claude loads honey in place from the repo" if in_place else "Claude runs the current repo code", f"Claude's copy at {where} differs from the repo (run /plugin update or reinstall)", warn=True)

    print("Codex")
    toml = ""
    try:
        with open(os.path.join(xdir, "config.toml"), encoding="utf-8") as f:
            toml = f.read()
    except FileNotFoundError:
        pass

    def enabled(p):
        m = re.search(r'\[plugins\."' + re.escape(p) + r'"\]\s*\n(?:(?!\[)[^\n]*\n)*?\s*enabled\s*=\s*(true|false)', toml)
        return m and m.group(1) == "true"

    rep.ok(enabled(PLUGIN), f"{PLUGIN} enabled", f"{PLUGIN} not enabled in config.toml")
    for p in CODEX_CLASH:
        rep.ok(not enabled(p), f"{p} disabled", f"{p} still enabled")
    from honey import codex_hooks
    trust = codex_hooks.inspect()
    rep.ok(trust['status'] == 'trusted', f"Codex trusts {trust.get('trusted', 0)} current honey hooks",
           f"Codex hook trust: {trust['status']}; review /hooks for the installed Honey version")
    caches = glob.glob(os.path.join(xdir, "plugins", "cache", "honey-stack", "honey", "*"))
    if caches:
        rep.ok(_tree_hash(caches[-1]) == _tree_hash(paths.REPO), "Codex cache matches repo", "Codex cache is stale: run honey sync", warn=True)
    else:
        rep.ok(False, "", "honey not in Codex plugin cache (run honey install)")
    abroken = [n for n in glob.glob(os.path.join(paths.agents_dir(), "skills", "*")) if os.path.islink(n) and not os.path.exists(n)]
    rep.ok(not abroken, "no dangling ~/.agents skill links", f"{len(abroken)} dangling links in ~/.agents/skills")

    print("Hooks and skills")
    mine = {os.path.basename(os.path.dirname(f)) for f in glob.glob(os.path.join(paths.REPO, "skills", "*", "SKILL.md"))}
    dups = sorted({os.path.basename(d) for base in (os.path.join(cdir, "skills"), os.path.join(paths.agents_dir(), "skills"), os.path.join(xdir, "skills"))
                   for d in glob.glob(os.path.join(base, "*")) if os.path.basename(d) in mine and os.path.exists(d)})
    rep.ok(not dups, "no skill-name duplicates outside honey", f"same-name skills outside honey: {dups} (honey install retires absorbed ones)", warn=True)
    ok, ms = smoke()
    rep.ok(ok, f"SessionStart smoke test ({ms} ms)", f"SessionStart smoke test failed ({ms} ms)")
    rep.ok(ms < 300, "hook latency fine", f"hook took {ms} ms", warn=True)
    total, users = skills_budget()
    rep.ok(total <= BUDGET, f"model-invoked descriptions {total} chars (≤{BUDGET}); {users} user-invoked skills cost 0",
           f"model-invoked descriptions {total} chars > {BUDGET}")
    import datetime as _dt
    stale = []
    for f in glob.glob(os.path.join(paths.REPO, "skills", "*", "SKILL.md")):
        m = re.search(r'^\s*reviewed:\s*"?(\d{4}-\d{2}-\d{2})', open(f, encoding="utf-8").read(), re.M)
        if m and (_dt.date.today() - _dt.date.fromisoformat(m.group(1))).days > 365:
            stale.append(os.path.basename(os.path.dirname(f)))
    rep.ok(not stale, "skill facts fresh (reviewed <1y)", f"skills with facts >1y old, refresh them: {stale}", warn=True)

    print("Browser QA")
    ab = agent_browser()
    rep.ok(bool(ab), f"agent-browser {ab} (qa uses it for live agent-driven checks)",
           "agent-browser not on PATH: qa falls back to Playwright MCP or e2e (optional: npm i -g agent-browser && agent-browser install)", warn=True)
    from honey import retired
    rep.ok(not os.path.isfile(retired.delta_plist()), "no retired Delta capture service",
           f"retired Delta capture service still installed at {retired.delta_plist()} (run honey install or honey delta to remove it)", warn=True)

    print("RSI")
    h = paths.home()
    errs = paths.path("logs", "hook-errors.log")
    rep.ok(not os.path.exists(errs), "no hook errors logged", f"hook errors logged in {errs}", warn=True)
    files = sorted(glob.glob(paths.path("events", "*", "*.jsonl")), key=os.path.getmtime)
    last = {}
    for fp in files[-20:]:
        for r in store.read_jsonl(fp):
            last[(r.get("a"), r.get("e"))] = r.get("ts")
    for agent in ("claude", "codex"):
        seen = sorted(e for (a, e) in last if a == agent)
        rep.ok(bool(seen), f"{agent}: events seen {', '.join(seen)}", f"{agent}: no events yet (start a session)", warn=True)
    remotes = subprocess.run(["git", "-C", h, "remote"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip() if os.path.isdir(os.path.join(h, ".git")) else ""
    rep.ok(not remotes, "~/.honey has no git remote (stays local)", f"~/.honey has a git remote: {remotes}")
    print(f"  info {len(review.pending())} items pending in /rsi review")
    print("all good" if not rep.fails else f"{rep.fails} problem(s)")
    return 1 if rep.fails else 0
