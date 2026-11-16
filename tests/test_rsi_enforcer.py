"""Repeated learnings get a suggested structural enforcer; it stays a prose rule until a proof is recorded."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch as mock

from helpers import REPO
from honey.rsi import cli, curate, text


def bullet(when, then, sessions=1, **extra):
    b = curate.new_bullet(when, then, "global", "pitfall", "observed", 2,
                          [{"sid": f"claude-s{i}", "q": "x"} for i in range(sessions)], "proposed")
    b.update(extra)
    return b


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.dict(os.environ, HONEY_HOME=self.tmp.name)
        env.start()
        self.addCleanup(env.stop)


class Heuristic(unittest.TestCase):
    def test_levels(self):
        cases = (("running git commands", "never force push to main", "guard"),
                 ("changing the parser", "run the tests; it broke regressions twice", "test"),
                 ("writing python", "use snake_case naming and black formatting", "lint"),
                 ("explaining a plan", "keep it to three sentences", "prose"))
        for when, then, level in cases:
            self.assertEqual(curate.suggest_enforcer({"when": when, "then": then})["level"], level, then)

    def test_only_repeated_learnings_get_a_suggestion(self):
        once, twice = bullet("running git commands", "never force push"), bullet("running git commands", "never force push", 2)
        curate.attach_enforcer(once)
        curate.attach_enforcer(twice)
        self.assertNotIn("enforcer", once)
        self.assertEqual(twice["enforcer"]["level"], "guard")
        self.assertEqual(twice["enforcer"]["applied"], "prose")


class Gate(unittest.TestCase):
    def test_old_learning_without_enforcer_is_prose(self):
        old = {"id": "b-old", "when": "always", "then": "be terse"}
        self.assertEqual(curate.enforcer(old), {"level": "prose", "suggestion": "", "proof": "", "applied": "prose"})
        self.assertEqual(curate.enforcer_line(old), "")
        self.assertEqual(curate.enforcer({"enforcer": "garbage"})["level"], "prose")

    def test_structure_requires_proof(self):
        claimed = {"enforcer": {"level": "test", "suggestion": "regression test", "applied": "structure"}}
        self.assertEqual(curate.enforcer(claimed)["applied"], "prose")
        proven = {"enforcer": {"level": "test", "suggestion": "regression test", "proof": "fails on s3 before fix"}}
        self.assertEqual(curate.enforcer(proven)["applied"], "structure")
        self.assertEqual(curate.enforcer({"enforcer": {"level": "prose", "proof": "x"}})["applied"], "prose")

    def test_enforcer_text_stays_out_of_brief_and_rule_line(self):
        b = bullet("running git commands", "never force push", 2)
        curate.attach_enforcer(b)
        self.assertNotIn("enforce", curate.line(b))


class Schema(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO, "honey", "rsi", "schemas", "reflection.schema.json"), encoding="utf-8") as f:
            self.cand = json.load(f)["properties"]["candidates"]["items"]

    def test_enforcer_is_nullable_and_strict(self):
        self.assertEqual(sorted(self.cand["required"]), sorted(self.cand["properties"]))  # Codex strict mode
        options = self.cand["properties"]["enforcer"]["anyOf"]
        self.assertIn({"type": "null"}, options)
        obj = next(o for o in options if o["type"] == "object")
        self.assertEqual(obj["properties"]["level"]["enum"], list(curate.ENFORCER_LEVELS))
        self.assertNotIn("proof", obj["properties"])  # the reflector can never claim proof


class Reflection(Isolated):
    def apply(self, **cand):
        c = {"op": "new", "target_id": "", "when": "running git commands", "then": "never force push to main",
             "kind": "pitfall", "scope": "global", "applicability": "", "severity": 2, "evidence": ["s1"],
             "counterfactual": "it would force push", "non_obvious": True, "durable": True, "material": True}
        c.update(cand)
        ids = {"s1": {"e": "prompt", "origin": "user", "q": "do not force push", "ts": text.now()}}
        curate.apply_reflection({"candidates": [c]}, "claude-a", ids, {"agent": "claude"})
        return curate.load()[0]

    def test_candidate_without_enforcer_is_accepted(self):
        self.assertNotIn("enforcer", self.apply())

    def test_null_enforcer_is_accepted(self):
        self.assertNotIn("enforcer", self.apply(enforcer=None))

    def test_first_time_proposal_waits_for_a_repeat(self):
        self.assertNotIn("enforcer", self.apply(enforcer={"level": "lint", "suggestion": "lint rule"}))

    def test_reflector_proposal_on_a_repeat_is_kept_without_proof(self):
        with mock.object(curate, "repeated", return_value=True):
            b = self.apply(enforcer={"level": "lint", "suggestion": "lint rule", "proof": "forged"})
        self.assertEqual(b["enforcer"], {"level": "lint", "suggestion": "lint rule", "proof": "", "applied": "prose"})


class Display(Isolated):
    def test_review_shows_one_enforcer_line(self):
        b = bullet("running git commands", "never force push to main", 2)
        curate.attach_enforcer(b)
        curate.save([b, bullet("explaining", "keep it short")])
        out = io.StringIO()
        with mock.object(cli.review, "audit", return_value=[]), contextlib.redirect_stdout(out):
            cli.list_review()
        lines = [ln.strip() for ln in out.getvalue().splitlines() if "enforce:" in ln]
        self.assertEqual(lines, ["enforce: guard - Add a honey guard that warns before the command this rule describes. "
                                 "(prose until proof)"])
        out = io.StringIO()
        with mock.object(cli.review, "audit", return_value=[]), contextlib.redirect_stdout(out):
            cli.list_review(as_json=True)
        enforce = {p["then"]: p["enforce"] for p in json.loads(out.getvalue())["proposed_learnings"]}
        self.assertEqual(enforce["keep it short"], "")
        self.assertTrue(enforce["never force push to main"].startswith("enforce: guard"))


class Proof(Isolated):
    def test_proof_turns_a_suggestion_into_structure(self):
        b = bullet("running git commands", "never force push to main", 2)
        curate.attach_enforcer(b)
        curate.save([b])
        with mock.object(cli.store, "git_commit"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["proof", b["id"], "guard", "denied", "git push -f origin main"]), 0)
        e = curate.get(b["id"])["enforcer"]
        self.assertEqual((e["level"], e["applied"]), ("guard", "structure"))
        self.assertIn("git push -f", e["proof"])

    def test_prose_learning_needs_a_level(self):
        b = bullet("explaining", "keep it short")
        curate.save([b])
        self.assertEqual(curate.record_proof(b["id"], "lint fails on long reply")["applied"], "prose")
        self.assertEqual(curate.record_proof(b["id"], "lint fails on long reply", "lint")["applied"], "structure")

    def test_unknown_id_and_empty_proof(self):
        self.assertIsNone(curate.record_proof("nope", "x"))
        b = bullet("explaining", "keep it short")
        curate.save([b])
        self.assertIsNone(curate.record_proof(b["id"], "   "))


class ReviewFixes(Isolated):
    def test_stored_prose_enforcer_is_upgraded_once_repeated(self):
        b = bullet("running git commands", "never force push to main", 3, enforcer={"level": "prose", "suggestion": ""})
        curate.attach_enforcer(b)
        self.assertEqual(b["enforcer"]["level"], "guard")

    def test_proven_prose_is_left_alone(self):
        b = bullet("running git commands", "never force push to main", 3,
                    enforcer={"level": "prose", "suggestion": "", "proof": "kept as prose on purpose"})
        curate.attach_enforcer(b)
        self.assertEqual(b["enforcer"]["level"], "prose")

    def test_cli_blank_evidence_is_its_own_error(self):
        b = bullet("explaining", "keep it short")
        curate.save([b])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["proof", b["id"], " "]), 1)
        self.assertIn("needs evidence", out.getvalue())


if __name__ == "__main__":
    unittest.main()
