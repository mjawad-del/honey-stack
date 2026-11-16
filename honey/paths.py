"""Filesystem locations. Everything honey writes lives under HONEY_HOME (default ~/.honey)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def home() -> str:
    return os.environ.get("HONEY_HOME") or os.path.join(os.path.expanduser("~"), ".honey")


def path(*parts: str) -> str:
    return os.path.join(home(), *parts)


def source_checkout(candidate, own=False):
    """Validate an editable Honey checkout, never an installed plugin cache."""
    if not isinstance(candidate, str) or not os.path.isabs(candidate):
        return None
    root = os.path.realpath(candidate)
    parts = root.split(os.sep)
    if any(parts[i:i + 2] == ["plugins", "cache"] for i in range(len(parts) - 1)):
        return None
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    if not own:
        try:
            with open(os.path.join(root, ".codex-plugin", "plugin.json"), encoding="utf-8") as f:
                marker = json.load(f)
            if marker.get("name") != "honey" or not os.path.isfile(os.path.join(root, "honey", "__init__.py")):
                return None
        except (OSError, ValueError, AttributeError):
            return None
    result = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
    top = os.path.normcase(os.path.realpath(result.stdout.strip())) if result.returncode == 0 else None
    return root if top == os.path.normcase(root) else None  # Windows git prints C:/…, realpath gives C:\…


def source_repo() -> str:
    """Resolve the editable checkout even when this runtime is an installed cache."""

    own = source_checkout(REPO, own=True)
    if own:
        return own
    try:
        with open(path("synced.json"), encoding="utf-8") as f:
            metadata = json.load(f)
        source = source_checkout(metadata.get("source_repo"))
    except (OSError, ValueError, AttributeError):
        source = None
    if not source:
        raise RuntimeError("Honey source checkout is unavailable; run honey sync from the source checkout first")
    return source


def safe_id(raw: str) -> str:
    """Session ids end up in file names; never let them traverse paths."""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))[:80] or "nosession"


def session_key(agent: str, sid: str) -> str:
    return f"{agent}-{safe_id(sid)}"


def project_slug(cwd: str) -> str:
    """owner-repo from the git remote, else the repo/dir basename. One git call, cached by caller."""
    cwd = cwd or os.getcwd()
    try:
        out = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2).stdout.strip()
    except Exception:
        out = ""
    if out:
        m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", out)
        if m:
            return re.sub(r"[^A-Za-z0-9_.-]", "", f"{m.group(1)}-{m.group(2)}").lower()
    base = os.path.basename(os.path.normpath(cwd)) or "root"
    return "nogit-" + re.sub(r"[^A-Za-z0-9_.-]", "", base).lower()[:40] + "-" + hashlib.sha1(cwd.encode()).hexdigest()[:6]


def shell_path(p: str) -> str:
    """A path for a shell command string. Claude Code runs commands in Git Bash on Windows; forward slashes work there."""
    return p.replace("\\", "/") if os.name == "nt" else p


def claude_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def codex_dir() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def agents_dir() -> str:
    return os.environ.get("HONEY_AGENTS_DIR") or os.path.join(os.path.expanduser("~"), ".agents")
