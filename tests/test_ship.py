"""`honey ship`: check, commit, push and open or reuse a PR in one call, against a real git remote and a fake gh."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from helpers import REPO

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_GH_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["pr", "view"]:
    pr = os.environ.get("FAKE_GH_PR")
    if not pr:
        print("no pull requests found", file=sys.stderr); sys.exit(1)
    print(json.dumps({"url": pr, "state": "OPEN"}))
elif sys.argv[1:3] == ["pr", "create"]:
    print("https://github.com/o/r/pull/9")
"""


class Ship(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        remote, self.repo = self.tmp / "remote.git", self.tmp / "repo"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.git("remote", "add", "origin", str(remote))
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "init")
        self.git("push", "-q", "-u", "origin", "main")
        self.git("switch", "-q", "-c", "feat/x")
        gh = self.tmp / "fake_gh.py"
        gh.write_text(FAKE_GH, encoding="utf-8")
        self.log = self.tmp / "gh.log"
        self.env = dict(os.environ, HONEY_GH=json.dumps([sys.executable, str(gh)]), FAKE_GH_LOG=str(self.log),
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def git(self, *args):
        return subprocess.run([*GIT, *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def ship(self, *args, **env):
        return subprocess.run([sys.executable, str(Path(REPO, "bin/honey")), "ship", *args], cwd=self.repo,
                              env=dict(self.env, **env), capture_output=True, text=True, encoding="utf-8")

    def gh_calls(self):
        return [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def test_commit_push_and_open_pr_in_one_call(self):
        (self.repo / "b.txt").write_text("b\n")
        (self.repo / "stray.txt").write_text("not mine\n")
        r = self.ship("-m", "feat(x): add b", "--body", "Why: b.", "--trailer", "Co-Authored-By: T <t@t>",
                      "--fixes", "55,58", "--pr", "b.txt")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("https://github.com/o/r/pull/9", r.stdout)
        msg = self.git("log", "-1", "--format=%B")
        self.assertTrue(msg.startswith("feat(x): add b"), msg)
        self.assertIn("Fixes #55", msg)
        self.assertIn("Co-Authored-By: T <t@t>", msg)
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD"), "b.txt")  # only the named path
        self.assertIn("stray.txt", self.git("status", "--porcelain"))
        self.assertEqual(self.git("rev-parse", "@{u}"), self.git("rev-parse", "HEAD"))  # pushed with upstream
        create = [c for c in self.gh_calls() if c[:2] == ["pr", "create"]][0]
        self.assertEqual(create[create.index("--base") + 1], "main")
        self.assertEqual(create[create.index("--title") + 1], "feat(x): add b")
        body = Path(create[create.index("--body-file") + 1])
        self.assertFalse(body.exists())  # temp body file is cleaned up

    def test_branch_tracking_main_pushes_to_its_own_name(self):
        self.git("switch", "-q", "-c", "feat/y", "--track", "origin/main")
        (self.repo / "b.txt").write_text("b\n")
        r = self.ship("-m", "fix: b", "b.txt")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "@{u}"), "origin/feat/y")
        self.assertNotEqual(self.git("rev-parse", "origin/main"), self.git("rev-parse", "HEAD"))  # main untouched

    def test_existing_pr_is_reused_not_duplicated(self):
        (self.repo / "b.txt").write_text("b\n")
        r = self.ship("-m", "fix: b", "--pr", "b.txt", FAKE_GH_PR="https://github.com/o/r/pull/3")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("pull/3", r.stdout)
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["pr", "create"]])

    def test_failed_check_commits_nothing(self):
        (self.repo / "b.txt").write_text("b\n")
        head = self.git("rev-parse", "HEAD")
        r = self.ship("-m", "fix: b", "--check", f"{sys.executable} -c 'raise SystemExit(3)'", "b.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_refuses_default_branch_and_empty_commit(self):
        self.git("switch", "-q", "main")
        (self.repo / "b.txt").write_text("b\n")
        r = self.ship("-m", "fix: b", "b.txt")
        self.assertEqual(r.returncode, 2)
        self.assertIn("default branch", r.stderr)
        self.git("switch", "-q", "feat/x")
        r = self.ship("-m", "fix: nothing")
        self.assertEqual(r.returncode, 2)
        self.assertIn("nothing staged", r.stderr)

    def test_rejected_push_never_forces(self):
        (self.repo / "b.txt").write_text("b\n")
        self.assertEqual(self.ship("-m", "fix: b", "b.txt").returncode, 0)
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", "-b", "feat/x", str(self.tmp / "remote.git"), str(other)], check=True)
        (other / "c.txt").write_text("c\n")
        subprocess.run([*GIT, "add", "c.txt"], cwd=other, check=True)
        subprocess.run([*GIT, "commit", "-qm", "c"], cwd=other, check=True)
        subprocess.run([*GIT, "push", "-q"], cwd=other, check=True)
        remote_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=other, capture_output=True, text=True).stdout
        (self.repo / "d.txt").write_text("d\n")
        r = self.ship("-m", "fix: d", "d.txt")
        self.assertEqual(r.returncode, 1)
        self.assertIn("push rejected", r.stderr)
        self.assertEqual(self.git("ls-remote", "origin", "feat/x").split()[0], remote_head.strip())


if __name__ == "__main__":
    unittest.main()
