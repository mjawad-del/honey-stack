"""Auto-update: a detached job started at SessionStart moves honey-stack to the newest vX.Y.Z release tag.

Only release tags reachable from origin/main ship, never raw main. A checkout with local edits, its own commits, or on
a branch other than main is left alone. Plugin-only installs (no checkout) go through the agents' own plugin CLIs.
Opt out with {"autoupdate": false} in ~/.honey/config.json. The running session keeps the code it started with; the
update loads next session.
"""
from __future__ import annotations

import os
import re
import subprocess
import time

from honey import paths
from honey.rsi import store

CHECK_EVERY = 3600       # seconds between checks; sessions starting inside the window skip the process and network
REPO_OVERRIDE = None     # tests point this at a throwaway checkout
RELEASE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$", re.ASCII)  # ASCII: \d would also accept lookalike Unicode digits
GIT_ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0")        # a background fetch must never wait on a credential prompt


def _git(repo: str, *args: str, timeout: int = 60):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=GIT_ENV)


def _key(tag: str):
    return tuple(int(x) for x in RELEASE.match(tag).groups())


def _release_tags(repo: str):
    releases = sorted((t for t in _git(repo, "tag", "--list", "v*").stdout.split() if RELEASE.match(t)), key=_key)
    return [t for t in releases if _git(repo, "merge-base", "--is-ancestor", t, "refs/remotes/origin/main").returncode == 0]


def enabled() -> bool:
    if os.environ.get("HONEY_NO_AUTOUPDATE"):
        return False
    return (store.read_json(paths.path("config.json"), {}) or {}).get("autoupdate", True) is not False


def _state_file() -> str:
    return paths.path("state", "autoupdate.json")


def due() -> bool:
    """Cheap check the hook runs before it spawns a process at all."""
    return enabled() and time.time() - (store.read_json(_state_file(), {}) or {}).get("checked", 0) >= CHECK_EVERY


def update_checkout(repo: str) -> dict:
    """Fast-forward honey's own git checkout to the newest release tag. Returns {status: updated|current|skipped, ...}."""
    top = _git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    if not top or os.path.realpath(top) != os.path.realpath(repo):
        return {"status": "skipped", "reason": "not honey-stack's own git root"}  # a copy inside someone else's repo
    if _git(repo, "fetch", "--tags", "--quiet", "origin").returncode != 0:
        return {"status": "skipped", "reason": "fetch failed (offline?)"}
    releases = _release_tags(repo)
    if not releases:
        return {"status": "skipped", "reason": "no release tags on origin/main"}
    newest = releases[-1]
    branch = _git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    at_release = RELEASE.match(_git(repo, "describe", "--tags", "--exact-match", "--match", "v[0-9]*").stdout.strip())
    if branch != "main" and not (not branch and at_release):  # main, or a detached HEAD sitting exactly on a release
        return {"status": "skipped", "reason": "not on main or a detached release tag"}
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return {"status": "skipped", "reason": "local changes"}
    if _git(repo, "merge-base", "--is-ancestor", newest, "HEAD").returncode == 0:
        if _git(repo, "merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/main").returncode != 0:
            return {"status": "skipped", "reason": "checkout has its own commits (not a fast-forward)"}
        return {"status": "current"}
    behind = [t for t in releases if _git(repo, "merge-base", "--is-ancestor", t, "HEAD").returncode == 0]
    current = behind[-1].lstrip("v") if behind else "?"
    if _git(repo, "merge", "--ff-only", "--quiet", newest).returncode != 0:
        return {"status": "skipped", "reason": "checkout has its own commits (not a fast-forward)"}
    return {"status": "updated", "from": current, "to": newest.lstrip("v")}


def _plugin_result(parent: str, current: str) -> dict:
    """Plugin caches hold one directory per version. A newer one than the running version means an update landed."""
    versions = sorted((d for d in os.listdir(parent) if RELEASE.match("v" + d)), key=lambda d: _key("v" + d))
    if versions and _key("v" + versions[-1]) > _key("v" + current):
        return {"status": "updated", "from": current, "to": versions[-1]}
    return {"status": "current"}


def _update_plugins(repo: str) -> dict:
    """Plugin-only install: no checkout to pull, so ask each agent's CLI to refresh the marketplace and plugin."""
    from honey import __version__, install
    ran, failed = False, []
    for agent, cmds in (("claude", [["plugin", "marketplace", "update", "honey-stack"], ["plugin", "update", install.PLUGIN]]),
                        ("codex", [["plugin", "marketplace", "upgrade", "honey-stack"], ["plugin", "add", install.PLUGIN]])):
        if not _agent_enabled(agent, repo):
            continue
        exe = install._cli(agent)
        if exe:
            ran = True
            for c in cmds:
                if subprocess.run([exe, *c], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, env=GIT_ENV).returncode != 0:
                    failed.append(agent)
                    break
    if not ran:
        return {"status": "skipped", "reason": "no agent CLI"}
    if failed:
        return {"status": "skipped", "reason": f"{', '.join(failed)} plugin refresh failed"}
    return _plugin_result(os.path.dirname(os.path.realpath(repo)), __version__)


def _in_plugin_cache(path: str) -> bool:
    parts = path.replace(os.sep, "/").split("/")
    return any(parts[i:i + 2] == ["plugins", "cache"] for i in range(len(parts) - 1))


def _codex_sections():
    """Read the relevant simple config sections without requiring a TOML dependency."""
    try:
        with open(os.path.join(paths.codex_dir(), "config.toml"), encoding="utf-8") as f:
            config = f.read()
    except OSError:
        return {}
    sections = re.split(r"(?m)^\s*\[([^\]]+)\]\s*(?:#.*)?$", config)
    return {name.strip(): body for name, body in zip(sections[1::2], sections[2::2])}


def _codex_marketplace_type(sections):
    for name, body in sections.items():
        if name.strip() in ("marketplaces.honey-stack", 'marketplaces."honey-stack"', "marketplaces.'honey-stack'"):
            match = re.search(r'''(?m)^\s*source_type\s*=\s*["'](local|git)["']\s*(?:#.*)?$''', body)
            return match.group(1) if match else None
    return None


def _agent_enabled(agent: str, repo: str) -> bool:
    """Do not install or enable Honey in another agent that does not use it."""
    from honey import install
    if agent == "claude":
        settings = store.read_json(os.path.join(paths.claude_dir(), "settings.json"), {}) or {}
        enabled = (settings.get("enabledPlugins") or {}).get(install.PLUGIN)
        cache = os.path.join(paths.claude_dir(), "plugins", "cache")
    else:
        sections = _codex_sections()
        plugin = sections.get('plugins."honey@honey-stack"', sections.get("plugins.'honey@honey-stack'", ""))
        flag = re.search(r"(?m)^\s*enabled\s*=\s*(true|false)\s*(?:#.*)?$", plugin)
        enabled = flag.group(1) == "true" if flag else None
        cache = os.path.join(paths.codex_dir(), "plugins", "cache")
    if enabled is not None:
        return bool(enabled)
    return os.path.commonpath([os.path.realpath(repo), os.path.realpath(cache)]) == os.path.realpath(cache)


def _local_install():
    """Find configured local marketplaces and confirm they reference the same checkout."""
    from honey import install
    try:
        recorded = paths.source_repo()
    except RuntimeError:
        recorded = None
    roots = {}
    codex = install._cli("codex")
    sections = _codex_sections()
    plugin = sections.get('plugins."honey@honey-stack"', sections.get("plugins.'honey@honey-stack'", ""))
    disabled = re.search(r"(?m)^\s*enabled\s*=\s*false\s*(?:#.*)?$", plugin)
    kind = _codex_marketplace_type(sections)
    if codex and not disabled and (kind == "local" or (recorded and kind != "git")):
        listing = subprocess.run([codex, "plugin", "marketplace", "list"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=180, env=GIT_ENV)
        if listing.returncode != 0:
            raise RuntimeError("cannot inspect the Codex marketplace")
        root = None
        for line in listing.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and fields[0] == "honey-stack":
                root = os.path.realpath(fields[1])
                break
        if kind == "local":
            if not paths.source_checkout(root):
                raise RuntimeError("Codex local marketplace is not an available Honey checkout")
            roots["codex"] = root
        elif root and root == recorded:
            roots["codex"] = recorded
    marketplaces = store.read_json(os.path.join(paths.claude_dir(), "plugins", "known_marketplaces.json"), {}) or {}
    entry = marketplaces.get("honey-stack") or {}
    origin = entry.get("source") or {}
    settings = store.read_json(os.path.join(paths.claude_dir(), "settings.json"), {}) or {}
    if (install._cli("claude") and (settings.get("enabledPlugins") or {}).get(install.PLUGIN)
            and origin.get("source") == "directory" and origin.get("path")):
        source = paths.source_checkout(origin["path"])
        if not source:
            raise RuntimeError("Claude local marketplace is not an available Honey checkout")
        roots["claude"] = source
    if len(set(roots.values())) > 1:
        raise RuntimeError("Claude and Codex marketplaces point to different local checkouts")
    return (next(iter(roots.values())), list(roots)) if roots else (None, [])


def _refresh_local(source: str, pending: dict, agents: list) -> dict:
    """Finish a checkout update; retain pending state until all installed copies refresh."""
    from honey import install
    if _git(source, "rev-parse", "HEAD").stdout.strip() != pending["head"]:
        return {"status": "skipped", "reason": "source checkout changed while plugin refresh was pending"}
    if _git(source, "status", "--porcelain").stdout.strip():
        return {"status": "skipped", "reason": "local changes block pending plugin refresh"}
    for agent in pending["agents"]:
        exe = install._cli(agent)
        if agent not in agents or not exe:
            return {"status": "skipped", "reason": f"{agent} local marketplace changed while refresh was pending"}
        cmd = ["plugin", "add", install.PLUGIN] if agent == "codex" else ["plugin", "update", install.PLUGIN]
        if subprocess.run([exe, *cmd], cwd=source, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, env=GIT_ENV).returncode != 0:
            return {"status": "skipped", "reason": f"{agent} plugin refresh failed; will retry"}
    store.write_json(paths.path("synced.json"), {"ts": time.strftime("%Y%m%dT%H%M%S"),
                                                 "head": pending["head"], "source_repo": source})
    return {"status": "updated", "from": pending["from"], "to": pending["to"]}


def run(force: bool = False) -> dict:
    """Run at most one update check at a time, including explicit checks."""
    if not enabled():
        return {"status": "skipped", "reason": "autoupdate off"}
    from honey import filelock
    lock_path = paths.path("state", "autoupdate.lock")
    try:
        store.ensure_dir(os.path.dirname(lock_path))
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if not filelock.try_lock(fd):
                return {"status": "skipped", "reason": "update check already in progress"}
            try:
                return _run(force)
            finally:
                filelock.unlock(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        return {"status": "skipped", "reason": f"update lock failed: {exc}"}


def _run(force: bool) -> dict:
    """The detached job, serialized by run()."""
    state = store.read_json(_state_file(), {}) or {}
    if not force and time.time() - state.get("checked", 0) < CHECK_EVERY:
        return {"status": "skipped", "reason": "checked recently"}
    state = dict(state, checked=time.time())
    store.write_json(_state_file(), state)  # stamp first so a crash cannot loop
    try:
        repo = REPO_OVERRIDE or paths.REPO
        if _in_plugin_cache(repo):
            source, agents = _local_install()
        elif not REPO_OVERRIDE:
            source, agents = _local_install()
            if source and os.path.realpath(repo) != source:
                raise RuntimeError("runtime and configured local marketplace use different checkouts")
            source = source or repo
        else:
            source, agents = repo, []  # isolated checkout override used in tests
        pending = state.get("pending_refresh")
        resumed = None
        if pending:
            if not source or source != pending["source"]:
                result = {"status": "skipped", "reason": "local marketplace changed while plugin refresh was pending"}
            else:
                result = _refresh_local(source, pending, agents)
                if force and result["status"] == "updated":
                    resumed = ({"status": "current", "refreshed": True} if pending.get("refresh_only") else result)
                    state.pop("pending_refresh", None)
                    store.write_json(_state_file(), state)
                    pending = None
        if not pending:
            if not source:
                result = _update_plugins(repo)
            else:
                result = update_checkout(source)
                if result["status"] == "updated" and agents:
                    head = _git(source, "rev-parse", "HEAD").stdout.strip()
                    pending = dict(result, source=source, head=head, agents=agents)
                    state["pending_refresh"] = pending
                    store.write_json(_state_file(), state)  # survive interruption after checkout moves
                    result = _refresh_local(source, pending, agents)
                elif force and result["status"] == "current" and agents and not resumed:
                    tags = _release_tags(source)
                    tag = tags[-1] if tags else None
                    head = _git(source, "rev-parse", "HEAD").stdout.strip()
                    tagged = _git(source, "rev-parse", tag + "^{commit}").stdout.strip() if tag else ""
                    manifest = store.read_json(os.path.join(source, ".codex-plugin", "plugin.json"), {}) or {}
                    if not tag or not head or tagged != head or not isinstance(manifest, dict) or manifest.get("version") != tag[1:]:
                        result = {"status": "skipped", "reason": "checkout is not at a matching release tag"}
                    else:
                        pending = {"source": source, "head": head,
                                   "agents": agents, "from": tag[1:], "to": tag[1:], "refresh_only": True}
                        state["pending_refresh"] = pending
                        store.write_json(_state_file(), state)
                        result = _refresh_local(source, pending, agents)
                if resumed and result["status"] == "current":
                    result = resumed
    except Exception as e:  # network, git or CLI trouble must never break a session
        result = {"status": "skipped", "reason": f"{type(e).__name__}: {e}"}
    state = dict(store.read_json(_state_file(), {}) or {}, last=result)
    if result["status"] == "updated" and pending and pending.get("refresh_only"):
        result = {"status": "current", "refreshed": True}
        state["last"] = result
    if result["status"] == "updated" or result.get("refreshed"):
        state.pop("pending_refresh", None)
    if result["status"] == "updated":
        state["pending_notice"] = result
    store.write_json(_state_file(), state)
    return result


def notice() -> str:
    """One line at the next SessionStart after an update, then cleared."""
    state = store.read_json(_state_file(), {}) or {}
    r = state.pop("pending_notice", None)
    if not r:
        return ""
    store.write_json(_state_file(), state)
    return f"honey-stack auto-updated {r['from']} → {r['to']}. Turn off: set \"autoupdate\": false in ~/.honey/config.json."
