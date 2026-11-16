"""Autonomous queue eligibility, failure isolation and fairness."""
import os
import tempfile
import unittest
from unittest.mock import patch

from helpers import REPO
from honey import paths
from honey.rsi import autonomy, curate, instructions, observe, reflect, review, store


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HONEY_HOME": self.tmp.name,
                                     "CLAUDE_CONFIG_DIR": os.path.join(self.tmp.name, "claude")})
        env.start()
        self.addCleanup(env.stop)
        autonomy.configure(True)
        self.doc = os.path.join(paths.claude_dir(), "CLAUDE.md")
        store.write_text(self.doc, "# mine\n")

    def instruction(self, key):
        p = review.add(1, "instr_line", key, "reason", "fix", [], key)
        p["edit"] = {"file": "claude", "action": "add", "line": "- remember " + key}
        self.save(p)
        return p

    def save(self, p):
        store.write_json(paths.path("proposals", p["id"] + ".json"), p)

    def run_queue(self):
        with store.lock():
            return autonomy.run()

    def test_forgotten_graduation_is_not_applied(self):
        b = curate.new_bullet("testing", "show test evidence", "global", "preference", "observed", 2, [], "active")
        curate.save([b])
        p = self.instruction("forgotten")
        p["edit"]["bullet"] = b["id"]
        self.save(p)
        curate.forget(b["id"])
        self.run_queue()
        with open(self.doc, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# mine\n")
        self.assertEqual(store.read_json(paths.path("proposals", p["id"] + ".json"))["status"], "pending")

    def test_blocked_items_do_not_starve_later_work(self):
        for i in range(autonomy.MAX_PER_RUN):
            p = review.add(1, "unknown", "blocked", "reason", "fix", [], str(i))
            p["created"] = "2000-01-01"
            self.save(p)
        good = self.instruction("later valid")
        self.run_queue()
        self.run_queue()
        self.assertEqual(store.read_json(paths.path("proposals", good["id"] + ".json"))["status"], "applied")

    def test_malformed_records_do_not_crash_the_queue(self):
        store.write_json(paths.path("proposals", "a-no-id.json"), {"status": "pending", "type": "unknown"})
        store.write_json(paths.path("proposals", "b-list.json"), ["wrong shape"])
        good = self.instruction("after malformed")
        result = self.run_queue()
        self.assertIn(good["id"], result["applied"])
        self.assertTrue(result["blocked"])

    def test_terminal_user_decisions_stay_terminal(self):
        for decision in ("skip", "never"):
            p = self.instruction(decision)
            review.decide(p["id"], decision)
        self.run_queue()
        with open(self.doc, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# mine\n")

    def test_catchup_does_not_reflect_paused_sessions(self):
        store.append_jsonl(observe.events_file("claude-paused"), {"e": "end"})
        store.write_json(paths.path("state", "sessions", "claude-paused.json"), {"rsi_off": True})
        self.assertEqual(reflect.due_sessions(), [])

    def test_audit_carries_sources_for_revalidation(self):
        b = curate.new_bullet("testing", "replace old instruction", "global", "preference", "observed", 2, [], "active")
        curate.save([b])
        result = {"items": [{"file": "claude", "action": "remove", "line": "# mine",
                             "replacement": "", "reason": "old", "bullet_ids": [b["id"]]}]}
        with patch.object(reflect, "call_llm", return_value=(result, 0, "fake")), \
             patch.object(reflect, "budget_block", return_value=None):
            items = instructions.llm_audit()
        self.assertEqual(items[0].get("bullet_ids"), [b["id"]])
        curate.forget(b["id"])
        self.run_queue()
        with open(self.doc, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# mine\n")

    def test_cached_runtime_statusline_uses_stable_launcher(self):
        from honey import install
        settings = os.path.join(paths.claude_dir(), "settings.json")
        store.write_json(settings, {"statusLine": {"type": "command", "command": "/plugins/cache/old/0.1.0/main.py"}})
        stable = os.path.join(self.tmp.name, "bin", "honey")
        store.write_text(stable, "# stable launcher\n")
        review.audit()
        with patch.object(paths, "REPO", "/plugins/cache/honey-stack/honey/0.9.1"), \
             patch.object(install, "_cli", return_value=stable):
            self.run_queue()
        self.assertEqual(store.read_json(settings)["statusLine"]["command"], f'python3 "{stable}" statusline')
        self.assertEqual(review.audit(), [])

    def test_legacy_audit_without_sources_requires_new_evidence(self):
        p = review.add(1, "instr_remove", "old removal", "old evidence", "remove", [], "legacy removal")
        p["edit"] = {"file": "claude", "action": "remove", "line": "# mine", "replacement": ""}
        self.save(p)
        result = self.run_queue()
        self.assertEqual(result["applied"], [])
        self.assertIn("source", result["blocked"][0]["reason"])
        with open(self.doc, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# mine\n")


if __name__ == "__main__":
    unittest.main()
