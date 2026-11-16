"""`honey ship`: check, commit, push and open (or reuse) a PR in one call, so shipping is one tool call
instead of six to eight. Safe by construction: stages only named paths, never forces a push, refuses the
default branch unless told otherwise, and commits nothing when the check fails."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

USAGE = "honey ship -m SUBJECT [--body TEXT] [--trailer LINE]... [--fixes 1,2] [--check CMD] [--pr] [paths...]"


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def _gh() -> list:
    """The gh command: HONEY_GH may name an executable or hold a JSON argv list (tests, Windows wrappers)."""
    raw = os.environ.get("HONEY_GH", "gh")
    return json.loads(raw) if raw.lstrip().startswith("[") else [raw]


def _git(*args) -> str:
    r = _run(["git", *args])
    return r.stdout.strip() if r.returncode == 0 else ""


def _default_branch() -> str:
    ref = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref:
        return ref.split("/", 1)[-1]
    for name in ("main", "master"):
        if _git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}"):
            return name
    return "main"


def _fail(msg: str, code: int = 1) -> int:
    print(f"honey ship: {msg}", file=sys.stderr)
    return code


def _message(a) -> str:
    parts = [a.message.strip()]
    if a.body:
        parts.append(a.body.strip())
    fixes = [f"Fixes #{n.strip().lstrip('#')}" for n in (a.fixes or "").split(",") if n.strip()]
    if fixes:
        parts.append("\n".join(fixes))
    if a.trailer:
        parts.append("\n".join(a.trailer))
    return "\n\n".join(parts) + "\n"


def main(argv: list) -> int:
    p = argparse.ArgumentParser(prog="honey ship", usage=USAGE, description=__doc__)
    p.add_argument("-m", "--message", required=True, help="commit subject, also the PR title by default")
    p.add_argument("--body", help="commit body")
    p.add_argument("--trailer", action="append", help="trailer line, e.g. 'Co-Authored-By: …' (repeatable)")
    p.add_argument("--fixes", help="issue numbers closed by this change, comma-separated")
    p.add_argument("--check", help="shell command that must pass before anything is committed")
    p.add_argument("--pr", action="store_true", help="open a PR, or report the open one for this branch")
    p.add_argument("--title", help="PR title (default: the subject)")
    p.add_argument("--pr-body-file", help="PR body (default: commit body and Fixes lines)")
    p.add_argument("--base", help="PR base branch (default: the remote's default branch)")
    p.add_argument("--draft", action="store_true")
    p.add_argument("--allow-default", action="store_true", help="permit committing on the default branch")
    p.add_argument("paths", nargs="*", help="paths to stage; already staged changes are included")
    a = p.parse_args(argv)

    branch = _git("branch", "--show-current")
    if not branch:
        return _fail("not on a branch (detached HEAD or not a git repo)", 2)
    default = _default_branch()
    if branch == default and not a.allow_default:
        return _fail(f"on the default branch {default!r}; create a branch first or pass --allow-default", 2)

    if a.check:
        r = subprocess.run(a.check, shell=True)
        if r.returncode != 0:
            return _fail(f"check failed (exit {r.returncode}); nothing committed")
    if a.paths:
        r = _run(["git", "add", "--", *a.paths])
        if r.returncode != 0:
            return _fail(r.stderr.strip() or "git add failed", 2)
    if _run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return _fail("nothing staged; name the paths to ship", 2)

    r = _run(["git", "commit", "-q", "-F", "-"], input=_message(a))
    if r.returncode != 0:
        return _fail("commit failed (a hook may have rejected it); nothing pushed\n" + (r.stdout + r.stderr).strip())
    print(f"committed {_git('rev-parse', '--short', 'HEAD')} {a.message.strip()}", flush=True)

    # Look up an existing PR while the push runs: the two network calls overlap.
    view = subprocess.Popen([*_gh(), "pr", "view", "--json", "url,state"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace") if a.pr else None
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.split("/", 1)[-1] != branch:  # none yet, or a branch cut from origin/main still tracking it
        upstream = ""
    push = _run(["git", "push", "-q"] if upstream else ["git", "push", "-q", "-u", "origin", "HEAD"])
    if push.returncode != 0:
        if view:
            view.kill()
        err = push.stderr.strip()
        if "rejected" in err or "non-fast-forward" in err or "fetch first" in err:
            return _fail("push rejected: the remote moved. Fetch, integrate and rerun checks; never force.\n" + err)
        return _fail("push failed\n" + err)
    print(f"pushed {branch} -> {upstream or 'origin/' + branch}")

    if not view:
        return 0
    out, _ = view.communicate()
    try:
        existing = json.loads(out) if view.returncode == 0 and out.strip() else None
    except ValueError:
        existing = None
    if existing and existing.get("state") == "OPEN":
        print(f"pr {existing['url']} (already open; body unchanged)")
        return 0

    body_file, tmp = a.pr_body_file, None
    if not body_file:
        fd, tmp = tempfile.mkstemp(prefix="honey-ship-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_message(argparse.Namespace(message="", body=a.body, fixes=a.fixes, trailer=None)).strip() + "\n")
        body_file = tmp
    try:
        cmd = [*_gh(), "pr", "create", "--base", a.base or default, "--title", a.title or a.message.strip(),
               "--body-file", body_file] + (["--draft"] if a.draft else [])
        r = _run(cmd)
    finally:
        if tmp:
            os.unlink(tmp)
    if r.returncode != 0:
        return _fail("pr create failed (commit and push succeeded)\n" + (r.stdout + r.stderr).strip())
    print(f"pr {r.stdout.strip().splitlines()[-1]} (opened)")
    return 0
