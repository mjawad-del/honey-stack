"""Status block: the Verdict line counts as a receipt, a done claim under a non-VERIFIED verdict is recorded,
the always-on rules ask for the block, and the subagent contract caps scope."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from honey import modes, receipt, tasks  # noqa: E402


def gap(msg):
    state = {"edited": True}
    assert not tasks.missing(state)  # no recorded task: these cases are about the receipt itself
    return receipt.check({"last_assistant_message": msg}, state)


class StatusBlock(unittest.TestCase):
    def test_verified_block_is_a_receipt(self):
        self.assertIsNone(gap("Added the toggle.\nVerdict: VERIFIED\nChecks run: pytest pass."))

    def test_done_claim_under_not_verified_is_recorded(self):
        g = gap("Fixed the footer.\nVerdict: NOT VERIFIED\nChecks run: none.")
        self.assertEqual(g[0], "verdict")
        self.assertIn("NOT VERIFIED", g[1])

    def test_inconclusive_without_done_claim_is_honest(self):
        self.assertIsNone(gap("Changed the parser; the suite could not start.\nVerdict: INCONCLUSIVE\nSkipped: pytest (no venv)."))

    def test_unresolved_wording_is_not_a_contradiction(self):
        self.assertIsNone(gap("Fixed the parser but could not run the suite.\nVerdict: INCONCLUSIVE"))

    def test_asks_done_under_inconclusive_is_recorded(self):
        msg = "Asks: 1 done\nVerdict: INCONCLUSIVE\nChecks run: none. Skipped: e2e (could not start browser)."
        self.assertEqual(gap(msg)[0], "verdict")
        self.assertEqual(gap("Asks: 1 done · 2 not done\nVerdict: NOT VERIFIED")[0], "verdict")

    def test_incomplete_asks_are_not_done_claims(self):
        for status in ("not done", "not yet done"):
            with self.subTest(status=status):
                self.assertIsNone(gap(f"Asks: 1 {status}\nVerdict: NOT VERIFIED\nChecks run: none."))

    def test_mixed_asks_still_detect_a_positive_done_claim(self):
        self.assertEqual(gap("Asks: 1 not yet done · 2 done\nVerdict: INCONCLUSIVE")[0], "verdict")

    def test_status_line_words_are_not_claims(self):
        self.assertIsNone(gap("Changed the parser.\nVerdict: INCONCLUSIVE\nRisk: removed fallback untested."))

    def test_bold_verdict_parses(self):
        self.assertEqual(gap("Fixed it.\n**Verdict:** NOT VERIFIED")[0], "verdict")

    def test_edit_turn_without_any_block_still_flagged(self):
        self.assertEqual(gap("Changed the parser.")[0], "receipt")

    def test_rules_ask_for_block_and_steps(self):
        text = modes.render("claude", {})
        for part in ("Shipped:", "Verdict: VERIFIED|NOT VERIFIED|INCONCLUSIVE", "Checks run:", "todo list verbatim", "n/a: <reason>"):
            self.assertIn(part, text)

    def test_subagent_contract_caps_scope_and_questions(self):
        self.assertIn("whole bar", modes.SUBAGENT)
        self.assertIn("Don't ask questions", modes.SUBAGENT)


if __name__ == "__main__":
    unittest.main()
