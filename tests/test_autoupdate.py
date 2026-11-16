"""Auto-update: at session start a detached job moves a git checkout to the newest vX.Y.Z release tag, never raw main."""
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("HONEY_NO_AUTOUPDATE", None)  # the runner sets it; this test exercises the feature
from honey import autoupdate, paths  # noqa: E402

G = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]


def git(cwd, *a):
    return subprocess.run([*G, *a], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()


def commit(cwd, name, tag=None):
    with open(os.path.join(cwd, name), "w", encoding="utf-8") as f:
        f.write(name)
    git(cwd, "add", name)
    git(cwd, "commit", "-qm", name)
    if tag:
        git(cwd, "tag", tag)
    return git(cwd, "rev-parse", "HEAD")


with tempfile.TemporaryDirectory() as d:
    origin = os.path.join(d, "origin")
    os.makedirs(origin)
    git(origin, "init", "-q", "-b", "main")
    v1 = commit(origin, "a", "v0.1.0")
    clone = os.path.join(d, "clone")
    subprocess.run([*G, "clone", "-q", origin, clone], check=True)

    # nothing newer: current
    assert autoupdate.update_checkout(clone)["status"] == "current"

    # a release, then unreleased work and junk tags after it: only the release is applied
    v2 = commit(origin, "b", "v0.2.0")
    commit(origin, "c")                      # untagged main commit must not ship
    git(origin, "tag", "v0.3.0-rc1")         # prerelease tag is not a release
    git(origin, "tag", "nightly")
    r = autoupdate.update_checkout(clone)
    assert r["status"] == "updated" and r["from"] == "0.1.0" and r["to"] == "0.2.0", r
    assert git(clone, "rev-parse", "HEAD") == v2

    # numeric, not lexical, ordering: 0.10.0 beats 0.9.0
    commit(origin, "d", "v0.9.0")
    commit(origin, "e", "v0.10.0")
    assert autoupdate.update_checkout(clone)["to"] == "0.10.0"

    # local edits block the update and are left alone
    commit(origin, "f", "v0.10.1")
    with open(os.path.join(clone, "a"), "w", encoding="utf-8") as f:
        f.write("mine")
    r = autoupdate.update_checkout(clone)
    assert r["status"] == "skipped" and "local changes" in r["reason"], r
    assert open(os.path.join(clone, "a"), encoding="utf-8").read() == "mine"
    git(clone, "checkout", "--", "a")

    # a diverged checkout is never rewritten
    before = commit(clone, "local")
    r = autoupdate.update_checkout(clone)
    assert r["status"] == "skipped" and git(clone, "rev-parse", "HEAD") == before, r

# review hardening
with tempfile.TemporaryDirectory() as d:
    origin = os.path.join(d, "o")
    os.makedirs(origin)
    git(origin, "init", "-q", "-b", "main")
    commit(origin, "a", "v1.0.0")
    clone = os.path.join(d, "c")
    subprocess.run([*G, "clone", "-q", origin, clone], check=True)
    tip = commit(origin, "b", "v1.1.0")
    git(origin, "tag", "v٩.٢.٣")                                       # Unicode-digit lookalike is not a release
    assert autoupdate.update_checkout(clone)["to"] == "1.1.0"          # fast-forwarded main
    # Local-only main commits are preserved and reported as blocking updates.
    own_head = commit(clone, "mine")
    result = autoupdate.update_checkout(clone)
    assert result["status"] == "skipped" and "own commits" in result["reason"], result
    assert git(clone, "rev-parse", "HEAD") == own_head
    # a feature branch is never moved, even when clean and behind
    git(clone, "checkout", "-q", "-b", "feature", "v1.0.0")
    r = autoupdate.update_checkout(clone)
    assert r["status"] == "skipped" and git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "feature", r
    # a detached HEAD sitting exactly on a release is a pinned install: it does move
    git(clone, "checkout", "-q", "--detach", "v1.0.0")
    assert autoupdate.update_checkout(clone)["status"] == "updated"
    # a local-only tag that origin/main does not contain is not a release
    git(clone, "checkout", "-q", "main"); git(clone, "reset", "-q", "--hard", "v1.1.0")
    commit(clone, "x", "v9.9.9")
    git(clone, "reset", "-q", "--hard", "v1.1.0")
    assert autoupdate.update_checkout(clone)["status"] == "current"
    # a checkout nested inside another repo is not honey's own root: never touched
    parent = os.path.join(d, "parent"); os.makedirs(parent)
    git(parent, "init", "-q", "-b", "main"); commit(parent, "p", "v5.0.0")
    nested = os.path.join(parent, "tools", "honey"); os.makedirs(nested)
    r = autoupdate.update_checkout(nested)
    assert r["status"] == "skipped" and "root" in r["reason"], r

# plugin-cache detection and result from versions on disk
assert autoupdate._in_plugin_cache("/h/.claude/plugins/cache/honey-stack/honey/0.9.0")
assert not autoupdate._in_plugin_cache("/h/src/honey-stack")
with tempfile.TemporaryDirectory() as d:
    for v in ("0.9.0", "0.10.0"):
        os.makedirs(os.path.join(d, v))
    assert autoupdate._plugin_result(d, "0.9.0") == {"status": "updated", "from": "0.9.0", "to": "0.10.0"}
    assert autoupdate._plugin_result(d, "0.10.0")["status"] == "current"

# run(): opt-out, throttle, one-time notice
H = os.environ["HONEY_HOME"]
os.makedirs(os.path.join(H, "state"), exist_ok=True)
with open(os.path.join(H, "config.json"), "w", encoding="utf-8") as f:
    json.dump({"autoupdate": False}, f)
assert autoupdate.run()["status"] == "skipped"
os.remove(os.path.join(H, "config.json"))

with tempfile.TemporaryDirectory() as d:
    origin = os.path.join(d, "o")
    os.makedirs(origin)
    git(origin, "init", "-q", "-b", "main")
    commit(origin, "a", "v1.0.0")
    clone = os.path.join(d, "c")
    subprocess.run([*G, "clone", "-q", origin, clone], check=True)
    commit(origin, "b", "v1.1.0")
    autoupdate.REPO_OVERRIDE = clone
    assert autoupdate.run()["status"] == "updated"
    assert autoupdate.run()["status"] == "skipped"                  # throttled: second session within the hour
    n = autoupdate.notice()
    assert "1.0.0" in n and "1.1.0" in n, n
    assert autoupdate.notice() == ""                                 # shown once

print("autoupdate ok")

# the real hook: SessionStart(startup) stays fast and quiet, and surfaces the notice once after an update
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ctx_text, hook  # noqa: E402

os.environ["HONEY_NO_AUTOUPDATE"] = "1"          # no detached job from this probe

with open(paths.path("state", "autoupdate.json"), "w", encoding="utf-8") as f:
    json.dump({"checked": time.time(), "pending_notice": {"from": "1.0.0", "to": "1.1.0"}}, f)
out = ctx_text(hook("claude", {"session_id": "au", "cwd": "/tmp", "hook_event_name": "SessionStart", "source": "startup"}))
assert "auto-updated 1.0.0 → 1.1.0" in out, out[-300:]
out2 = ctx_text(hook("claude", {"session_id": "au2", "cwd": "/tmp", "hook_event_name": "SessionStart", "source": "startup"}))
assert "auto-updated" not in out2
print("autoupdate hook ok")
