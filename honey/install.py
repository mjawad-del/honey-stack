"""honey install | uninstall | sync — reversible, idempotent, dry-run first.

Every change is planned in memory, shown as a diff, then applied and recorded in ~/.honey/install-manifest.json
with what was there before. Uninstall replays the manifest backwards. Moved things go to ~/.honey/backups/<ts>/;
nothing is deleted except dangling symlinks (whose targets are recorded so uninstall can recreate them).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

from honey import paths
from honey.rsi import review, store

MCT_HOOKS = ("session-context.py", "mct-prompt-router.py", "completion-receipt.py", "pre-compact-note.py",
             "guard-dangerous-command.py", "post-edit-sanity.py")
CLAUDE_CLASH = ("superpowers@claude-plugins-official", "ponytail@ponytail")
CODEX_CLASH = ("superpowers@openai-curated", "ponytail@ponytail", "pstack@cursor-plugins")
PLUGIN = "honey@honey-stack"
# Phase 3: things honey-stack now owns. Moved to backups (never deleted); uninstall moves them back.
MCT_SKILLS = ("code-review", "debugging-loop", "design-consistency", "docs-maintainer", "frontend-architecture", "frontend-ui-ux-harness",
              "git-pr-workflow", "nextjs-app-router", "performance-audit", "react-quality", "refactor-safely", "security-sanity",
              "testing-strategy", "ui-system", "have-i-missed-something", "opensrc", "todo-orchestrator", "react-doctor")
MCT_COMMANDS = ("commit-ready", "debug-bug", "docs-sync", "fix-review", "implement-plan", "install-project-automation", "mct-audit",
                "mct-commit", "mct-done", "mct-next", "mct-self-update", "mct-start", "mct-status", "memory-save", "opensrc",
                "plan-feature", "pr-ready", "react-doctor", "review-diff", "todo-next", "ui-check")
MCT_AGENTS = ("frontend-implementer", "planner", "react-doctor-runner", "reviewer", "test-runner", "todo-orchestrator")
CODEX_DUP_SKILLS = ("diagnosing-bugs", "domain-modeling", "handoff", "implement", "prototype", "to-spec", "to-tickets", "wayfinder")
MATT_AGENTS_SKILLS = ("ask-matt", "grill-me", "grill-with-docs", "grilling", "setup-matt-pocock-skills")
READ_ONLY_AGENTS = ("planner", "reviewer")


class Env:
    def __init__(self, home_override=None, only=None):
        self.sandbox = bool(home_override)
        h = os.path.abspath(home_override) if home_override else os.path.expanduser("~")
        self.claude = os.path.join(h, ".claude") if self.sandbox else paths.claude_dir()
        self.codex = os.path.join(h, ".codex") if self.sandbox else paths.codex_dir()
        self.agents = os.path.join(h, ".agents") if self.sandbox else paths.agents_dir()
        self.localbin = os.path.join(h, ".local", "bin")
        self.honey = os.path.join(h, ".honey") if self.sandbox else paths.home()
        self.only = only
        self.slim = False      # opt-in: replace CLAUDE.md / AGENTS.md with migrate/ templates (author's personal prefs by default)
        self.retire = False    # opt-in: move absorbed MCT / Matt Pocock / duplicate skills to backups
        self.budget_fraction = None  # opt-in: raise Claude skillListingBudgetFraction for more description room
        self.stamp = time.strftime("%Y%m%dT%H%M%S")
        self.backups = os.path.join(self.honey, "backups", self.stamp)
        self.manifest = os.path.join(self.honey, "install-manifest.json")

    def want(self, agent):
        return self.only in (None, agent)


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _read(p: str):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _diff(path: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff((old or "").splitlines(True), new.splitlines(True), path + " (before)", path + " (after)", n=2))


def _cli(name: str):
    for c in (shutil.which(name), os.path.expanduser(f"~/.local/bin/{name}"), f"/opt/homebrew/bin/{name}"):
        if c and os.path.exists(c):
            return c
    return None


# ---- planning -------------------------------------------------------------------------------------------------

def _plan_settings(env: Env, steps: list) -> None:
    fp = os.path.join(env.claude, "settings.json")
    raw = _read(fp)
    if raw is None:
        return
    s = json.loads(raw)
    removed = []
    for event in list((s.get("hooks") or {}).keys()):
        groups = []
        for g in s["hooks"][event] or []:
            keep = [h for h in g.get("hooks") or [] if not any(m in h.get("command", "") for m in MCT_HOOKS)]
            for h in g.get("hooks") or []:
                if h not in keep:
                    removed.append({"event": event, "matcher": g.get("matcher"), "hook": h})
            if keep:
                groups.append(dict(g, hooks=keep))
        if groups:
            s["hooks"][event] = groups
        else:
            del s["hooks"][event]
    plugins_before = {}
    for p in CLAUDE_CLASH:
        if (s.get("enabledPlugins") or {}).get(p):
            plugins_before[p] = True
            s["enabledPlugins"][p] = False
    sl_before = s.get("statusLine")
    sl_new = {"type": "command", "command": f'python3 "{os.path.join(paths.REPO, "bin", "honey")}" statusline'}
    if sl_before != sl_new:
        s["statusLine"] = sl_new
    new = json.dumps(s, indent=2, ensure_ascii=False) + "\n"
    if new != raw:
        steps.append({"kind": "edit_json", "file": fp, "before_sha": _sha(raw), "new": new, "old": raw,
                      "undo": {"removed_hooks": removed, "plugins": plugins_before, "statusLine": sl_before},
                      "summary": f"settings.json: remove {len(removed)} MCT hook handlers, disable {list(plugins_before)}, statusLine → honey"})


def _plan_toml(env: Env, steps: list) -> None:
    fp = os.path.join(env.codex, "config.toml")
    raw = _read(fp)
    if raw is None:
        return
    new, flipped = raw, []
    for p in CODEX_CLASH:
        rx = re.compile(r'(\[plugins\."' + re.escape(p) + r'"\]\s*\n(?:(?!\[)[^\n]*\n)*?\s*enabled\s*=\s*)true\b')
        new, n = rx.subn(r"\1false", new)
        if n:
            flipped.append(p)
    if flipped:
        steps.append({"kind": "edit_toml", "file": fp, "before_sha": _sha(raw), "new": new, "old": raw,
                      "undo": {"plugins": flipped}, "summary": f"config.toml: disable {flipped}"})


def _plan_instruction(env: Env, steps: list, target: str, template: str) -> None:
    raw = _read(target)
    new = _read(os.path.join(paths.REPO, "migrate", template))
    if raw == new:
        return
    if raw is None:
        steps.append({'kind': 'write_new', 'path': target, 'content': new, 'old': None,
                      'summary': f'{target}: create personal instruction template'})
        return
    steps.append({"kind": "replace_file", "file": target, "new": new, "old": raw,
                  "backup": os.path.join(env.backups, os.path.basename(os.path.dirname(target)), os.path.basename(target)),
                  "summary": f"{target}: slim to personal preferences (original backed up)"})


def _plan_activation(env: Env, steps: list, target: str, agent: str) -> None:
    from honey import activation
    replacement = activation.block(os.path.join(env.localbin, 'honey'), agent)
    prior = next((step for step in steps if (step.get('file') or step.get('path')) == target
                  and step['kind'] in ('replace_file', 'write_new')), None)
    if prior:
        field = 'new' if prior['kind'] == 'replace_file' else 'content'
        prior[field], _ = activation.replace_block(prior[field], replacement)
        if prior[field] == prior['old']:
            steps.remove(prior)
        return
    original = _read(target)
    updated, old_block = activation.replace_block(original or '', replacement)
    if updated != original:
        steps.append({'kind': 'instruction_block', 'file': target, 'old': original, 'new': updated,
                      'block': replacement, 'old_block': old_block,
                      'summary': f'{target}: activate global Honey bootstrap (preserve personal instructions)'})


def _plan_broken_links(steps: list, d: str) -> None:
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        return
    for n in names:
        fp = os.path.join(d, n)
        if os.path.islink(fp) and not os.path.exists(fp):
            steps.append({"kind": "unlink", "path": fp, "target": os.readlink(fp), "summary": f"remove dangling link {fp}"})


def _plan_retire(env: Env, steps: list) -> None:
    moving = []
    for d, names, ext, tag in ((os.path.join(env.claude, "skills"), MCT_SKILLS, "", "claude-skills"),
                               (os.path.join(env.claude, "commands"), MCT_COMMANDS, ".md", "claude-commands"),
                               (os.path.join(env.claude, "agents"), MCT_AGENTS, ".md", "claude-agents"),
                               (os.path.join(env.codex, "skills"), CODEX_DUP_SKILLS, "", "codex-skills"),
                               (os.path.join(env.agents, "skills"), MATT_AGENTS_SKILLS, "", "agents-skills")):
        if (tag.startswith("codex") and not env.want("codex")) or (tag.startswith("claude") and not env.want("claude")):
            continue
        for n in names:
            src = os.path.join(d, n + ext)
            if os.path.exists(src) and not os.path.islink(src):
                moving.append(os.path.realpath(src))
                steps.append({"kind": "move", "src": src, "dst": os.path.join(env.backups, tag, n + ext),
                              "summary": f"retire {tag}/{n + ext} (absorbed into honey-stack; moved to backups)"})
    sk = os.path.join(env.claude, "skills")
    for n in sorted(os.listdir(sk)) if os.path.isdir(sk) else []:
        fp = os.path.join(sk, n)
        if os.path.islink(fp) and os.path.exists(fp) and os.path.realpath(fp) in moving:
            steps.append({"kind": "unlink", "path": fp, "target": os.readlink(fp), "summary": f"remove link {fp} (its target is being retired)"})


def _toml_str(s: str) -> str:
    return '"""' + s.replace("\\", "\\\\").replace('"""', '\\"\\"\\"') + '"""'


def _plan_codex_agents(env: Env, steps: list) -> None:
    import glob as _glob
    for fp in sorted(_glob.glob(os.path.join(paths.REPO, "agents", "*.md"))):
        name = os.path.basename(fp)[:-3]
        with open(fp, encoding="utf-8") as stream:
            raw = stream.read()
        fm, body = (raw.split("---", 2)[1], raw.split("---", 2)[2]) if raw.startswith("---") else ("", raw)
        m = re.search(r"^description:\s*(.*)$", fm, re.M)
        desc = (m.group(1).strip().strip('"') if m else name)[:300]
        toml = (f'name = "honey-{name}"\ndescription = {json.dumps(desc)}\n'
                + ('sandbox_mode = "read-only"\n' if name in READ_ONLY_AGENTS else "")
                + f"developer_instructions = {_toml_str(body.strip())}\n")
        dst = os.path.join(env.codex, "agents", f"honey-{name}.toml")
        if _read(dst) != toml:
            steps.append({"kind": "write_new", "path": dst, "content": toml, "old": _read(dst),
                          "summary": f"write Codex agent {dst}"})


def plan(env: Env) -> list:
    steps = []
    if env.want("claude"):
        cs = store.read_json(os.path.join(env.claude, "settings.json"), {}) or {}
        if not env.sandbox and not (cs.get("enabledPlugins") or {}).get(PLUGIN):
            steps.append({"kind": "cli", "agent": "claude", "summary": f"claude plugin marketplace add + install {PLUGIN}",
                          "cmds": [["plugin", "marketplace", "add", paths.REPO], ["plugin", "install", PLUGIN]],
                          "undo_cmds": [["plugin", "uninstall", PLUGIN], ["plugin", "marketplace", "remove", "honey-stack"]]})
        _plan_settings(env, steps)
        _plan_broken_links(steps, os.path.join(env.claude, "skills"))
        if env.slim:
            _plan_instruction(env, steps, os.path.join(env.claude, "CLAUDE.md"), "CLAUDE.md")
        _plan_activation(env, steps, os.path.join(env.claude, 'CLAUDE.md'), 'claude')
        mct = os.path.join(env.claude, "MCT.md")
        if env.retire and os.path.exists(mct):
            steps.append({"kind": "move", "src": mct, "dst": os.path.join(env.backups, "claude", "MCT.md"),
                          "summary": f"retire {mct} (moved to backups)"})
        if env.budget_fraction:
            fp = os.path.join(env.claude, "settings.json")
            raw = _read(fp)
            if raw is not None:
                d = json.loads(raw)
                if d.get("skillListingBudgetFraction") != env.budget_fraction:
                    new = json.dumps(dict(d, skillListingBudgetFraction=env.budget_fraction), indent=2) + "\n"
                    steps.append({"kind": "edit_json", "file": fp, "before_sha": _sha(raw), "new": new, "old": raw,
                                  "undo": {"budget_fraction": d.get("skillListingBudgetFraction")},
                                  "summary": f"settings.json: skillListingBudgetFraction -> {env.budget_fraction} (more room for skill descriptions)"})
    if env.want("codex"):
        if not env.sandbox and f'[plugins."{PLUGIN}"]' not in (_read(os.path.join(env.codex, "config.toml")) or ""):
            steps.append({"kind": "cli", "agent": "codex", "summary": f"codex plugin marketplace add + add {PLUGIN}",
                          "cmds": [["plugin", "marketplace", "add", paths.REPO], ["plugin", "add", PLUGIN]],
                          "undo_cmds": [["plugin", "remove", PLUGIN], ["plugin", "marketplace", "remove", "honey-stack"]]})
        _plan_toml(env, steps)
        if env.slim:
            _plan_instruction(env, steps, os.path.join(env.codex, "AGENTS.md"), "AGENTS.md")
        _plan_activation(env, steps, os.path.join(env.codex, 'AGENTS.md'), 'codex')
        _plan_broken_links(steps, os.path.join(env.agents, "skills"))
        if os.path.isdir(os.path.join(paths.REPO, "agents")):
            _plan_codex_agents(env, steps)
    if env.retire:
        _plan_retire(env, steps)
    link = os.path.join(env.localbin, 'honey')
    if not os.path.lexists(link):
        try:
            source = paths.source_repo()
        except RuntimeError:
            source = paths.REPO
        steps.append({'kind': 'symlink', 'path': link, 'target': os.path.join(source, 'bin', 'honey'),
                      'summary': f'link {link} → source bin/honey'})
    return steps


# ---- apply / undo ---------------------------------------------------------------------------------------------

def _write_preserving_mode(fp: str, data: str) -> None:
    mode = os.stat(fp).st_mode & 0o777 if os.path.exists(fp) else 0o644
    tmp = fp + ".honey-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, fp)


def _apply(env: Env, st: dict) -> dict:
    k = st["kind"]
    rec = {k2: v for k2, v in st.items() if k2 not in ("new", "old", "content")}
    if k == "cli":
        exe = _cli(st["agent"])
        if not exe:
            raise RuntimeError(f"{st['agent']} CLI not found")
        for c in st["cmds"]:
            r = subprocess.run([exe, *c], capture_output=True, text=True, timeout=180)
            if r.returncode != 0 and "already" not in (r.stdout + r.stderr).lower():
                raise RuntimeError(f"{st['agent']} {' '.join(c)} failed: {(r.stderr or r.stdout)[-400:]}")
    elif k == 'instruction_block':
        if _read(st['file']) != st['old']:
            raise RuntimeError(f"{st['file']} changed since planning; re-run")
        os.makedirs(os.path.dirname(st['file']), exist_ok=True)
        if st['old'] is not None:
            backup = os.path.join(env.backups, 'global-activation', os.path.basename(os.path.dirname(st['file'])), os.path.basename(st['file']))
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            shutil.copy2(st['file'], backup)
            rec['backup'] = backup
        _write_preserving_mode(st['file'], st['new'])
        rec['written_sha'] = _sha(st['new'])
    elif k in ("edit_json", "edit_toml", "replace_file"):
        cur = _read(st["file"])
        if cur != st["old"]:
            raise RuntimeError(f"{st['file']} changed since planning; re-run")
        bk = st.get("backup") or os.path.join(env.backups, os.path.basename(os.path.dirname(st["file"])), os.path.basename(st["file"]))
        os.makedirs(os.path.dirname(bk), exist_ok=True)
        shutil.copy2(st["file"], bk)
        _write_preserving_mode(st["file"], st["new"])
        rec.update(backup=bk, written_sha=_sha(st["new"]))
    elif k == "unlink":
        os.unlink(st["path"])
    elif k == "move":
        os.makedirs(os.path.dirname(st["dst"]), exist_ok=True)
        shutil.move(st["src"], st["dst"])
    elif k == "symlink":
        os.makedirs(os.path.dirname(st["path"]), exist_ok=True)
        os.symlink(st["target"], st["path"])
    elif k == "write_new":
        os.makedirs(os.path.dirname(st["path"]), exist_ok=True)
        if st.get("old") is not None:
            bk = os.path.join(env.backups, "codex-agents", os.path.basename(st["path"]))
            os.makedirs(os.path.dirname(bk), exist_ok=True)
            shutil.copy2(st["path"], bk)
            rec["backup"] = bk
        with open(st["path"], "w", encoding="utf-8") as f:
            f.write(st["content"])
        rec.pop("content", None)
        rec["written_sha"] = _sha(st["content"])
    return rec


def _undo_json(rec: dict) -> str:
    s = json.loads(_read(rec["file"]))
    u = rec["undo"]
    for p, v in u.get("plugins", {}).items():
        s.setdefault("enabledPlugins", {})[p] = v
    if "budget_fraction" in u:
        if u["budget_fraction"] is None:
            s.pop("skillListingBudgetFraction", None)
        else:
            s["skillListingBudgetFraction"] = u["budget_fraction"]
    if "statusLine" in u:
        if u["statusLine"] is None:
            s.pop("statusLine", None)
        else:
            s["statusLine"] = u["statusLine"]
    for r in u.get("removed_hooks", []):
        g = {"hooks": [r["hook"]]}
        if r.get("matcher") is not None:
            g = {"matcher": r["matcher"], "hooks": [r["hook"]]}
        s.setdefault("hooks", {}).setdefault(r["event"], []).append(g)
    return json.dumps(s, indent=2, ensure_ascii=False) + "\n"


def _undo(rec: dict) -> str:
    k = rec["kind"]
    if k == 'instruction_block':
        current = _read(rec['file'])
        if current is None:
            return f"already absent: {rec['file']}"
        if _sha(current) == rec['written_sha']:
            if rec.get('backup'):
                shutil.copy2(rec['backup'], rec['file'])
            else:
                os.unlink(rec['file'])
        elif current.count(rec['block']) == 1:
            _write_preserving_mode(rec['file'], current.replace(rec['block'], rec.get('old_block') or '', 1))
        else:
            return f"global activation block changed; preserved {rec['file']}"
        return f"removed global Honey activation from {rec['file']}"
    if k == "cli":
        exe = _cli(rec["agent"])
        for c in rec["undo_cmds"]:
            if exe:
                subprocess.run([exe, *c], capture_output=True, text=True, timeout=180)
        return f"ran {rec['agent']} {' / '.join(' '.join(c) for c in rec['undo_cmds'])}"
    if k in ("edit_json", "edit_toml", "replace_file"):
        cur = _read(rec["file"])
        if cur is not None and _sha(cur) == rec.get("written_sha"):
            shutil.copy2(rec["backup"], rec["file"])
            return f"restored {rec['file']} from backup"
        if k == "edit_json" and cur is not None:
            _write_preserving_mode(rec["file"], _undo_json(rec))
            return f"reverted honey's keys in {rec['file']} (file had later edits; kept them)"
        if k == "edit_toml" and cur is not None:
            new = cur
            for p in rec["undo"]["plugins"]:
                new = re.sub(r'(\[plugins\."' + re.escape(p) + r'"\]\s*\n(?:(?!\[)[^\n]*\n)*?\s*enabled\s*=\s*)false\b', r"\1true", new)
            _write_preserving_mode(rec["file"], new)
            return f"re-enabled {rec['undo']['plugins']} in {rec['file']}"
        side = rec["file"] + ".honey-restore"
        shutil.copy2(rec["backup"], side)
        return f"{rec['file']} was edited after install; original saved beside it as {side}"
    if k == "unlink":
        if not os.path.lexists(rec["path"]):
            os.symlink(rec["target"], rec["path"])
        return f"recreated link {rec['path']}"
    if k == "move":
        if os.path.exists(rec["dst"]) and not os.path.exists(rec["src"]):
            shutil.move(rec["dst"], rec["src"])
        return f"moved back {rec['src']}"
    if k == "symlink":
        if os.path.islink(rec["path"]):
            os.unlink(rec["path"])
        return f"removed {rec['path']}"
    if k == "write_new":
        cur = _read(rec["path"])
        if rec.get("backup"):
            shutil.copy2(rec["backup"], rec["path"])
        elif cur is not None and _sha(cur) == rec.get("written_sha"):
            os.unlink(rec["path"])
        return f"removed {rec['path']}"
    return "?"


# ---- commands -------------------------------------------------------------------------------------------------

def _confirm(yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print("Not a terminal: re-run with --yes to apply, or --dry-run to preview.")
        return False
    return input("Apply these changes? [y/N] ").strip().lower() in ("y", "yes")


def install(env: Env, dry: bool, yes: bool) -> int:
    steps = plan(env)
    if not steps:
        print("Nothing to do: honey-stack is already installed.")
        return 0
    for i, st in enumerate(steps, 1):
        print(f"{i:2}. {st['summary']}")
    if dry:
        for st in steps:
            if "new" in st:
                print("\n" + _diff(st["file"], st["old"], st["new"]))
        print("\nDry run: nothing changed.")
        return 0
    if not _confirm(yes):
        return 1
    man = store.read_json(env.manifest, {"actions": []}) or {"actions": []}
    done = []
    try:
        for st in [s for s in steps if s["kind"] == "cli"]:
            done.append(_apply(env, st))
            print(f"  done: {st['summary']}")
        if done:  # agent CLIs edit settings.json/config.toml themselves; re-plan file edits against the new state
            steps = [s for s in plan(env) if s["kind"] != "cli"]
        for st in [s for s in steps if s["kind"] != "cli"]:
            done.append(_apply(env, st))
            print(f"  done: {st['summary']}")
    finally:
        man["actions"].extend(done)
        man["updated"] = env.stamp
        os.makedirs(env.honey, exist_ok=True)
        with open(env.manifest, "w", encoding="utf-8") as f:
            json.dump(man, f, indent=1)
    if not env.sandbox:
        prev = os.environ.get("HONEY_HOME")
        os.environ["HONEY_HOME"] = env.honey
        review.audit()
        store.git_commit("honey install")
        if prev is None:
            os.environ.pop("HONEY_HOME", None)
    print("\nInstalled. Next: start a new Claude session (or /reload-plugins). For Codex, open `codex`, run /hooks and trust the honey entries once.")
    return 0


def _summary(action: dict) -> str:
    """Older RSI repair records were written without a summary."""
    return action.get("summary") or f"{action.get('kind', 'change')} {action.get('file') or action.get('path') or ''}".strip()


def uninstall(env: Env, dry: bool, yes: bool, purge: bool) -> int:
    man = store.read_json(env.manifest, {"actions": []}) or {"actions": []}
    acts = [a for a in man["actions"] if env.want(a.get("agent")) or "agent" not in a]
    if not acts:
        print("Nothing to undo.")
        return 0
    for i, a in enumerate(reversed(acts), 1):
        print(f"{i:2}. undo: {_summary(a)}")
    if dry:
        print("\nDry run: nothing changed.")
        return 0
    if not _confirm(yes):
        return 1
    for a in reversed(acts):
        try:
            print("  " + _undo(a))
        except Exception as e:  # keep going; report what could not be undone
            print(f"  FAILED to undo {_summary(a)}: {e}")
    man["actions"] = [a for a in man["actions"] if a not in acts]
    with open(env.manifest, "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)
    if purge:
        shutil.rmtree(env.honey, ignore_errors=True)
        print(f"  purged {env.honey}")
    return 0


def sync(env: Env) -> int:
    """Refresh Codex's cache and record the source checkout only after success."""
    exe = _cli("codex")
    if not exe:
        print("codex CLI not found")
        return 1
    source = paths.source_repo()
    head = subprocess.run(["git", "-C", source, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    if head.returncode != 0 or not head.stdout.strip():
        print("cannot resolve Honey source HEAD")
        return 1
    r = subprocess.run([exe, "plugin", "add", PLUGIN], capture_output=True, text=True, timeout=180)
    print((r.stdout or r.stderr).strip())
    if r.returncode != 0:
        return r.returncode
    store.write_json(os.path.join(env.honey, "synced.json"), {"ts": env.stamp, "head": head.stdout.strip(), "source_repo": source})
    print("Claude picks up changes at the next session or /reload-plugins.")
    return 0


def update(env: Env) -> int:
    """Pull the latest release into this checkout, then refresh Codex's copy. Claude reads the checkout directly."""
    from honey import __version__ as before
    r = subprocess.run(["git", "-C", paths.REPO, "pull", "--ff-only", "--tags", "-q"], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        print("git pull failed (local changes or diverged branch?):\n" + (r.stderr or r.stdout).strip())
        return 1
    after = re.search(r'__version__ = "([^"]+)"', _read(os.path.join(paths.REPO, "honey", "__init__.py")) or "")
    after = after.group(1) if after else "?"
    print(f"honey-stack {before} → {after}" if after != before else f"honey-stack {after} is already the latest")
    if _cli("codex"):
        sync(env)
    print("Claude: start a new session or run /reload-plugins to load it.")
    return 0


def main(cmd: str, argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog=f"honey {cmd}")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--only", choices=["claude", "codex"])
    ap.add_argument("--home", help="sandbox home dir for a full rehearsal (no agent CLIs are called)")
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--slim-instructions", action="store_true",
                    help="replace ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md with migrate/ templates (edit them first; originals backed up)")
    ap.add_argument("--retire-legacy", action="store_true",
                    help="move skills/commands/agents honey-stack absorbs (MCT, Matt Pocock duplicates) to backups")
    ap.add_argument("--skill-budget", type=float, default=None, metavar="FRACTION",
                    help="set Claude skillListingBudgetFraction (e.g. 0.02) for more skill-description room; reversible")
    a = ap.parse_args(argv)
    env = Env(a.home, a.only)
    env.slim, env.retire = a.slim_instructions, a.retire_legacy
    env.budget_fraction = a.skill_budget
    if cmd == "install":
        return install(env, a.dry_run, a.yes)
    if cmd == "uninstall":
        return uninstall(env, a.dry_run, a.yes, a.purge)
    if cmd == "update":
        return update(env)
    return sync(env)
