"""Offline fixture/JS-grader checks; these do not run or prove model behavior."""
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

from helpers import REPO
from honey.rsi import behavioral


@unittest.skipIf(os.name == 'nt', 'eval fixtures are bash setup scripts for the POSIX eval harness')
class MaintainedFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.evals = Path(REPO, 'evals')

    def scaffold(self, name):
        subprocess.run(['bash', str(self.evals / name / 'setup.sh')], cwd=self.work,
                       check=True, capture_output=True)

    def matches(self, name, grader, filename):
        body = (self.evals / name / 'graders' / grader).read_text(encoding="utf-8")
        pattern = json.loads(next(line[9:] for line in body.splitlines() if line.startswith('pattern: ')))
        path = self.work / filename
        return path.exists() and bool(re.search(pattern, path.read_text(encoding="utf-8")))

    def test_readonly_grader_detects_file_mutation(self):
        name = 'behavior-preserve-readonly'; self.scaffold(name)
        self.assertTrue(self.matches(name, 'state.md', 'settings.json'))
        (self.work / 'settings.json').write_text('{}', encoding="utf-8")
        self.assertFalse(self.matches(name, 'state.md', 'settings.json'))

    def test_edit_grader_accepts_correct_json_and_requires_unchanged_check(self):
        name = 'behavior-scoped-config-edit'; self.scaffold(name)
        self.assertFalse(self.matches(name, 'state.md', 'settings.json'))
        settings = json.loads((self.work / 'settings.json').read_text(encoding="utf-8"))
        settings['startup']['editor'] = False
        (self.work / 'settings.json').write_text(json.dumps(settings, indent=2), encoding="utf-8")
        self.assertTrue(self.matches(name, 'state.md', 'settings.json'))
        self.assertTrue(self.matches(name, 'verifier.md', 'verify.py'))
        check = subprocess.run(['python3', 'verify.py'], cwd=self.work, capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertTrue(self.matches(name, 'success.md', 'verified-state.txt'))
        (self.work / 'verify.py').write_text('print("verified")', encoding="utf-8")
        self.assertFalse(self.matches(name, 'verifier.md', 'verify.py'))

    def test_failed_pre_edit_check_does_not_prove_later_edit(self):
        name = 'behavior-scoped-config-edit'; self.scaffold(name)
        check = subprocess.run(['python3', 'verify.py'], cwd=self.work, capture_output=True)
        self.assertNotEqual(check.returncode, 0)
        settings = json.loads((self.work / 'settings.json').read_text(encoding="utf-8"))
        settings['startup']['editor'] = False
        (self.work / 'settings.json').write_text(json.dumps(settings), encoding="utf-8")
        self.assertTrue(self.matches(name, 'state.md', 'settings.json'))
        self.assertFalse(self.matches(name, 'success.md', 'verified-state.txt'))

    @unittest.skipUnless(shutil.which('node'), 'optional JavaScript regex compatibility requires Node')
    def test_javascript_regex_compatibility(self):
        name = 'behavior-scoped-config-edit'; self.scaffold(name)
        for grader, filename in [('verifier.md', 'verify.py')]:
            body = (self.evals / name / 'graders' / grader).read_text(encoding="utf-8")
            pattern = json.loads(next(line[9:] for line in body.splitlines() if line.startswith('pattern: ')))
            result = subprocess.run(['node', '-e', 'process.exit(new RegExp(process.argv[1]).test(process.argv[2]) ? 0 : 1)',
                                     pattern, (self.work / filename).read_text(encoding="utf-8")], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_check_is_actually_nonzero(self):
        name = 'behavior-failed-verification'; self.scaffold(name)
        check = subprocess.run(['python3', 'verify.py'], cwd=self.work, capture_output=True)
        self.assertNotEqual(check.returncode, 0)
        self.assertTrue(self.matches(name, 'verifier.md', 'verify.py'))
        self.assertFalse((self.work / 'release-ready.txt').exists())

    def test_manifest_identifies_actual_supported_runner(self):
        evidence = behavioral.snapshot(REPO)
        self.assertEqual(evidence['runner'], 'claude-plugin-eval')
        self.assertEqual(evidence['clients'], ['claude'])
        self.assertEqual(len(evidence['cases']), 3)


if __name__ == '__main__': unittest.main()
