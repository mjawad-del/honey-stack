"""Autonomy job integration, pause gates and honest application bookkeeping."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from helpers import REPO
from honey import dispatch, paths
from honey.rsi import autonomy, cli, review, store, weekly


class AutonomyJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        env = {"HONEY_HOME": os.path.join(root, "honey"),
               "CLAUDE_CONFIG_DIR": os.path.join(root, "claude"),
               "CODEX_HOME": os.path.join(root, "codex"),
               "HONEY_AGENTS_DIR": os.path.join(root, "agents")}
        self.environment = patch.dict(os.environ, env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.doc = os.path.join(paths.claude_dir(), "CLAUDE.md")
        store.write_text(self.doc, "# personal\n")

    def proposal(self, kind="instr_line"):
        item = review.add(1, kind, "prove completion", "correction", "add proof rule", [], kind)
        if kind == "instr_line":
            item["edit"] = {"file": "claude", "action": "add", "line": "- WHEN finishing → show proof"}
            store.write_json(paths.path("proposals", item["id"] + ".json"), item)
        return item

    def run_weekly(self):
        # Only external generation/fetch boundaries are replaced. Queue, locking,
        # instruction validation, file application and decision records stay real.
        with patch.object(weekly.instructions, "llm_audit", return_value=[]), \
             patch.object(weekly.patch, "run_weekly", return_value=[]), \
             patch.object(weekly.radar, "run", return_value=[]):
            return weekly.run()

    def test_weekly_drains_when_enabled(self):
        item = self.proposal()
        autonomy.configure(True)
        self.run_weekly()
        saved = store.read_json(paths.path("proposals", item["id"] + ".json"))
        self.assertEqual(saved["status"], "applied")
        self.assertEqual(saved["decision_source"], "automatic")
        with open(self.doc, encoding="utf-8") as f:
            self.assertIn("WHEN finishing → show proof", f.read())
        self.assertEqual(store.read_jsonl(paths.path("golden.jsonl")), [])

    def test_weekly_leaves_pending_when_disabled(self):
        item = self.proposal()
        autonomy.configure(False)
        self.run_weekly()
        self.assertEqual(store.read_json(paths.path("proposals", item["id"] + ".json"))["status"], "pending")
        with open(self.doc, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# personal\n")

    def test_paused_hooks_do_not_spawn(self):
        os.makedirs(paths.path("events"), exist_ok=True)
        for flag in ("off", "rsi_off"):
            for event in ("SessionStart", "SessionEnd"):
                with self.subTest(flag=flag, event=event):
                    ctx = dispatch.Ctx("claude", {"session_id": flag + event,
                                                  "hook_event_name": event, "cwd": self.tmp.name})
                    ctx.state = {"started": "2026-10-18T00:00:00Z", "project": "test", flag: True}
                    with patch.object(dispatch.observe, "spawn") as spawn:
                        dispatch.HANDLERS[event](ctx)
                    spawn.assert_not_called()

    def snapshot(self):
        result = {}
        for root, _, files in os.walk(paths.home()):
            for name in files:
                if name == ".lock":
                    continue
                fp = os.path.join(root, name)
                with open(fp, "rb") as f:
                    result[os.path.relpath(fp, paths.home())] = f.read()
        return result

    def test_busy_weekly_skips_without_writing(self):
        self.proposal()
        autonomy.configure(True)
        with store.lock():
            before = self.snapshot()
            self.assertEqual(self.run_weekly(), {"skipped": "locked"})
            self.assertEqual(self.snapshot(), before)

    def test_unsupported_manual_repair_is_not_applied(self):
        item = self.proposal("toolkit_fix")
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                cli.decide(item["id"], "apply", "")
            except (SystemExit, ValueError):
                pass
        saved = store.read_json(paths.path("proposals", item["id"] + ".json"))
        self.assertEqual(saved["status"], "pending")
        self.assertIsNone(saved["decided"])

    def test_automatic_decisions_do_not_become_golden_feedback(self):
        item = self.proposal()
        autonomy.configure(True)
        with store.lock():
            result = autonomy.run()
        self.assertEqual(result["applied"], [item["id"]])
        self.assertEqual(store.read_jsonl(paths.path("golden.jsonl")), [])
        saved = store.read_json(paths.path("proposals", item["id"] + ".json"))
        self.assertEqual(saved["decision_source"], "automatic")


    def applied_proposal(self):
        item = self.proposal()
        autonomy.configure(True)
        with store.lock():
            autonomy.run()
        return item

    def cli_json(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main(list(args))
        return json.loads(output.getvalue())

    def test_status_reports_autonomy_and_recent_result(self):
        item = self.applied_proposal()
        result = self.cli_json("status", "--json")
        self.assertIs(result.get("autonomous"), True)
        self.assertIn(item["id"], result["recent_autonomy"]["applied"])

    def test_review_reports_recent_automatic_changes(self):
        item = self.applied_proposal()
        result = self.cli_json("review", "--json")
        self.assertIn(item["id"], result["recent_autonomy"]["applied"])

    def test_autonomous_session_notice_is_informational(self):
        self.applied_proposal()
        self.proposal("toolkit_fix")  # unsupported pending diagnosis must not ask for approval
        ctx = dispatch.Ctx("claude", {"session_id": "notice", "hook_event_name": "SessionStart",
                                      "source": "resume", "cwd": self.tmp.name})
        ctx.state = {"started": "2026-10-18T00:00:00Z", "project": "test"}
        result = dispatch.session_start(ctx)
        content = result["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("waiting in /rsi review", content)
        self.assertIn("autonom", content.lower())

    def toolkit_settings(self, settings):
        fp = os.path.join(paths.claude_dir(), "settings.json")
        store.write_json(fp, settings)
        proposals = review.audit()
        self.assertEqual(len(proposals), 1)
        return fp, proposals[0]

    def apply_toolkit(self, item):
        autonomy.configure(True)
        with store.lock():
            result = autonomy.run()
        self.assertIn(item["id"], result["applied"])
        return store.read_json(paths.path("proposals", item["id"] + ".json"))

    def assert_settings_backup(self, saved, original):
        backup = saved["repair_actions"][0]["backup"]
        self.assertEqual(store.read_json(backup), original)

    def test_statusline_repair_preserves_settings_and_backup(self):
        original = {"keep": {"enabled": True}, "statusLine": {"type": "command",
                    "command": "python /plugins/cache/old/tool/0.1.0/status.py"}}
        fp, item = self.toolkit_settings(original)
        saved = self.apply_toolkit(item)
        updated = store.read_json(fp)
        self.assertEqual(updated["keep"], original["keep"])
        self.assertEqual(updated["statusLine"]["command"], 'python3 "' + os.path.join(REPO, "bin", "honey") + '" statusline')
        self.assert_settings_backup(saved, original)

    def test_plugin_repair_preserves_unrelated_plugins_and_backup(self):
        original = {"keep": 42, "enabledPlugins": {"superpowers@claude-plugins-official": True,
                    "ponytail@ponytail": True, "my-plugin@example": True}}
        fp, item = self.toolkit_settings(original)
        saved = self.apply_toolkit(item)
        updated = store.read_json(fp)
        self.assertEqual(updated["keep"], 42)
        self.assertEqual(updated["enabledPlugins"], {"superpowers@claude-plugins-official": False,
                         "ponytail@ponytail": False, "my-plugin@example": True})
        self.assert_settings_backup(saved, original)

    @unittest.skipIf(os.name == 'nt', 'broken skill symlinks are a POSIX install artifact')
    def test_broken_link_repair_preserves_live_link_and_undo_target(self):
        directory = os.path.join(paths.claude_dir(), "skills")
        os.makedirs(directory)
        broken = os.path.join(directory, "broken")
        missing = os.path.join(self.tmp.name, "missing")
        os.symlink(missing, broken)
        live = os.path.join(directory, "live")
        os.symlink(self.doc, live)
        item, = review.audit()
        saved = self.apply_toolkit(item)
        self.assertFalse(os.path.lexists(broken))
        self.assertEqual(os.readlink(live), self.doc)
        action, = saved["repair_actions"]
        self.assertEqual(action["path"], broken)
        self.assertEqual(action["target"], missing)
        self.assertEqual(action["kind"], "unlink")

    def test_changed_statusline_blocks_without_overwriting(self):
        fp, item = self.toolkit_settings({"statusLine": {"type": "command",
                       "command": "python /plugins/cache/old/tool/0.1.0/status.py"}})
        current = {"statusLine": {"type": "command", "command": "my-new-status"}, "keep": 99}
        store.write_json(fp, current)
        autonomy.configure(True)
        with store.lock():
            result = autonomy.run()
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["blocked"][0]["id"], item["id"])
        self.assertIn("changed since audit", result["blocked"][0]["reason"])
        self.assertEqual(store.read_json(fp), current)
        saved = store.read_json(paths.path("proposals", item["id"] + ".json"))
        self.assertEqual(saved["status"], "pending")
        self.assertFalse(saved.get("repair_actions"))

    def test_changed_plugin_setting_blocks_without_overwriting(self):
        fp, item = self.toolkit_settings({"enabledPlugins": {"ponytail@ponytail": True}})
        current = {"enabledPlugins": {"ponytail@ponytail": False, "new@example": True}, "keep": 9}
        store.write_json(fp, current)
        autonomy.configure(True)
        with store.lock():
            result = autonomy.run()
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["blocked"][0]["id"], item["id"])
        self.assertEqual(store.read_json(fp), current)

    @unittest.skipIf(os.name == 'nt', 'broken skill symlinks are a POSIX install artifact')
    def test_changed_broken_link_blocks_and_preserves_new_target(self):
        directory = os.path.join(paths.claude_dir(), "skills")
        os.makedirs(directory)
        link = os.path.join(directory, "broken")
        os.symlink(os.path.join(self.tmp.name, "missing"), link)
        item, = review.audit()
        os.unlink(link)
        os.symlink(self.doc, link)
        autonomy.configure(True)
        with store.lock():
            result = autonomy.run()
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["blocked"][0]["id"], item["id"])
        self.assertEqual(os.readlink(link), self.doc)
        self.assertTrue(os.path.isfile(link))

if __name__ == "__main__":
    unittest.main()
