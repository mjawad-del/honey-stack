"""agent-browser integration in qa, doctor detection, and the clean retirement of honey delta."""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import REPO

ROOT = Path(REPO)
HONEY = [sys.executable, str(ROOT / "bin" / "honey")]
REFERENCE = ROOT / "skills/qa/references/agent-browser.md"
FIXTURE = ROOT / "evals/webmcp/benchmark/server.mjs"


def agent_browser_bin():
    local = ROOT / "evals/agent-browser/node_modules/.bin/agent-browser"
    return str(local) if local.exists() else shutil.which("agent-browser")


def reference_commands():
    block = re.search(r"```sh\n(.*?)```", REFERENCE.read_text(encoding="utf-8"), re.S)[1]
    return [line.split()[1] for line in block.splitlines() if line.startswith("agent-browser ")]


class QaSkill(unittest.TestCase):
    def test_qa_routes_live_browsing_to_the_reference(self):
        skill = (ROOT / "skills/qa/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("(references/agent-browser.md)", skill)
        self.assertIn("prefer `agent-browser` when it is installed", skill)
        self.assertIn("references/agent-browser.md", skill.split("---", 2)[1])  # cited in sources

    def test_reference_keeps_the_fallback_and_safety_rules(self):
        text = REFERENCE.read_text(encoding="utf-8")
        for rule in ("command -v agent-browser", "Playwright MCP or e2e", "user's approval",
                     "fresh snapshot", "close", "existing e2e suite"):
            with self.subTest(rule=rule):
                self.assertIn(rule, text)
        self.assertEqual(reference_commands(), ["open", "snapshot", "click", "fill", "select", "snapshot", "screenshot", "close"])

    def test_reference_commands_exist_in_the_cli(self):
        exe = agent_browser_bin()
        if not exe:
            self.skipTest("agent-browser not installed (npm ci in evals/agent-browser)")
        help_text = subprocess.run([exe, "--help"], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
        for command in set(reference_commands()):
            with self.subTest(command=command):
                self.assertRegex(help_text, rf"(?m)^\s+{command}\b")
        self.assertIn("--session", help_text)

    def test_benchmark_harness_parses(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        r = subprocess.run(["node", "--check", str(ROOT / "evals/agent-browser/run.mjs")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)


class LiveFlow(unittest.TestCase):
    """Runs the reference's command sequence against the real fixture in a real browser."""

    def test_reference_flow_in_a_real_browser(self):
        exe = agent_browser_bin()
        if not exe or not shutil.which("node") or os.environ.get("HONEY_SKIP_BROWSER"):
            self.skipTest("agent-browser or node not installed")
        server = subprocess.Popen(["node", str(FIXTURE)], stdout=subprocess.PIPE, text=True, encoding="utf-8")
        session = f"honey-test-{os.getpid()}"
        out = tempfile.mkdtemp()
        try:
            url = server.stdout.readline().strip().split("=", 1)[1]

            def ab(*args):
                r = subprocess.run([exe, *args, "--session", session], capture_output=True, text=True, encoding="utf-8", timeout=60)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                return r.stdout

            def ref(snapshot, role, name):
                return "@" + re.search(rf'{role} "{re.escape(name)}".*?ref=(e\d+)', snapshot)[1]

            self.assertIn("Task desk", ab("open", url))
            s = ab("snapshot", "-i")
            ab("click", ref(s, "button", "New task"))
            s = ab("snapshot", "-i")
            ab("fill", ref(s, "textbox", "Title"), "Ship report")
            ab("select", ref(s, "combobox", "Priority"), "High")
            ab("click", ref(s, "button", "Save task"))
            s = ab("snapshot", "-i")
            self.assertIn("Complete Ship report", s)
            ab("fill", ref(s, "searchbox", "Search tasks"), "Ship")
            s = ab("snapshot")
            self.assertNotIn("Plan sprint", s)
            # The fixture's seeded defect: every task is saved as Normal. A live check must surface it.
            self.assertIn("Priority: Normal", s)
            shot = os.path.join(out, "state.png")
            ab("screenshot", shot)
            with open(shot, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
        finally:
            subprocess.run([exe, "close", "--session", session], capture_output=True, timeout=30)
            server.kill()
            server.wait()
            server.stdout.close()
            shutil.rmtree(out, ignore_errors=True)


class Doctor(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'fake CLI fixture is an extensionless script; Windows only runs .exe/.cmd')
    def test_detects_agent_browser_version_on_path(self):
        from honey import doctor
        bindir = tempfile.mkdtemp()
        fake = os.path.join(bindir, "agent-browser")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho 'agent-browser 9.8.7'\n")
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        old = os.environ["PATH"]
        try:
            os.environ["PATH"] = bindir
            self.assertEqual(doctor.agent_browser(), "9.8.7")
            os.environ["PATH"] = tempfile.mkdtemp()
            self.assertEqual(doctor.agent_browser(), "")
        finally:
            os.environ["PATH"] = old


class DeltaRetired(unittest.TestCase):
    def test_no_delta_code_or_skill_ships(self):
        for path in ("honey/delta.py", "honey/delta_capture.py", "honey/delta_handoff.py", "honey/delta_service.py",
                     "skills/delta", "docs/delta.md"):
            with self.subTest(path=path):
                self.assertFalse((ROOT / path).exists())
        for name in ("README.md", "docs/skills.md", "skills/honey/SKILL.md", "skills/handoff/SKILL.md"):
            with self.subTest(doc=name):
                self.assertNotRegex((ROOT / name).read_text(encoding="utf-8"), r"`delta`|honey delta|delta\.dev")

    def test_delta_command_explains_the_removal(self):
        home = tempfile.mkdtemp()
        r = subprocess.run(HONEY + ["delta", "status"], capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, HOME=home))
        self.assertEqual(r.returncode, 2)
        self.assertIn("honey delta was removed", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("delta", subprocess.run(HONEY + ["--help"], capture_output=True, text=True, encoding="utf-8").stdout)

    def test_retired_launch_agent_is_moved_to_backups(self):
        from honey import retired
        home, backups = tempfile.mkdtemp(), tempfile.mkdtemp()
        plist = retired.delta_plist(home)
        os.makedirs(os.path.dirname(plist))
        Path(plist).write_text("<plist/>", encoding="utf-8")
        moved = retired.retire_delta_service(home=home, backups=backups)
        self.assertFalse(os.path.exists(plist))
        self.assertEqual(Path(moved).read_text(encoding="utf-8"), "<plist/>")
        self.assertIsNone(retired.retire_delta_service(home=home, backups=backups))

    def test_install_retires_the_launch_agent_and_uninstall_keeps_it_retired(self):
        home = tempfile.mkdtemp()
        for d in (".claude/skills", ".codex", ".agents/skills"):
            os.makedirs(os.path.join(home, d))
        plist = Path(home, "Library/LaunchAgents/dev.honey.delta-capture.plist")
        plist.parent.mkdir(parents=True)
        plist.write_text("<plist/>", encoding="utf-8")
        r = subprocess.run(HONEY + ["install", "--home", home, "--yes"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("honey delta was removed", r.stdout)
        self.assertFalse(plist.exists())
        r = subprocess.run(HONEY + ["uninstall", "--home", home, "--yes"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(plist.exists())
        backups = list(Path(home, ".honey/backups").rglob("dev.honey.delta-capture.plist"))
        self.assertEqual([b.read_text(encoding="utf-8") for b in backups], ["<plist/>"])


if __name__ == "__main__":
    unittest.main()
