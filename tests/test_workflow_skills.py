"""New workflow entry points must be discoverable without increasing implicit scope."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = ("project-verify", "triage", "research", "statechart", "gain")


class WorkflowSkills(unittest.TestCase):
    def test_explicit_entry_points_are_registered_on_both_hosts(self):
        router = (ROOT / "skills/honey/SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in ENTRY_POINTS:
            with self.subTest(skill=name):
                directory = ROOT / "skills" / name
                skill = (directory / "SKILL.md").read_text(encoding="utf-8")
                frontmatter = skill.split("---", 2)[1]
                self.assertRegex(frontmatter, rf"(?m)^name: {name}$")
                self.assertIn("disable-model-invocation: true", frontmatter)
                self.assertIn("license: MIT", frontmatter)
                self.assertIn("sources:", frontmatter)
                metadata = (directory / "agents/openai.yaml").read_text(encoding="utf-8")
                self.assertIn("allow_implicit_invocation: false", metadata)
                description = re.search(r'short_description: "([^"]+)"', metadata)
                self.assertIsNotNone(description)
                self.assertTrue(25 <= len(description[1]) <= 64)
                self.assertIn(f"`{name}`", router)
                self.assertIn(f"`{name}`", readme)

    def test_workflow_reference_links_are_shipped(self):
        directories = ENTRY_POINTS + ("frontend", "qa", "perf", "rsi", "write-skill")
        for name in directories:
            for path in (ROOT / "skills" / name).rglob("*.md"):
                # Links in fenced examples are instructions for generated files,
                # not dependencies of the distributed skill itself.
                prose = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
                for target in re.findall(r"\]\(([^)]+)\)", prose):
                    if "://" in target or target.startswith(("#", "/")):
                        continue
                    with self.subTest(source=str(path), target=target):
                        self.assertTrue((path.parent / target.split("#")[0]).exists())

    def test_gain_figures_come_from_the_cited_reports(self):
        gain = (ROOT / "skills/gain/SKILL.md").read_text(encoding="utf-8")
        webmcp = (ROOT / "evals/webmcp/reports/2026-11-01-fast-qa-v3.md").read_text(encoding="utf-8")
        browser = (ROOT / "evals/agent-browser/reports/2026-11-14-agent-qa.md").read_text(encoding="utf-8")
        for figure, report in (("7.209s", webmcp), ("3.396s", webmcp), ("2.12×", webmcp),
                               ("≈270", browser), ("≈1,231", browser), ("4.6×", browser), ("3.13s", browser)):
            with self.subTest(figure=figure):
                self.assertIn(figure, report)
        for shown in ("7.21s", "3.40s", "2.12×", "≈270", "≈1,231", "4.6×", "3.13s"):
            self.assertIn(shown, gain)
        self.assertIn("Never print a per-repo saving", gain)

    def test_review_and_testing_route_to_architecture_without_auto_invoking_it(self):
        for path in ("skills/review/SKILL.md", "agents/reviewer.md", "skills/tdd/SKILL.md", "skills/test-design/SKILL.md"):
            with self.subTest(path=path):
                self.assertIn("/honey:architecture", (ROOT / path).read_text(encoding="utf-8"))
        self.assertIn("architecture:", (ROOT / "skills/review/SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("missing seam", (ROOT / "skills/test-design/SKILL.md").read_text(encoding="utf-8"))
        frontmatter = (ROOT / "skills/architecture/SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
        self.assertIn("disable-model-invocation: true", frontmatter)

    def test_handoff_no_longer_depends_on_delta(self):
        handoff = (ROOT / "skills/handoff/SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("delta", handoff.lower())
        self.assertFalse((ROOT / "skills/delta").exists())


if __name__ == "__main__":
    unittest.main()
