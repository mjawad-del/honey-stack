"""Generated evals use the evaluator's regex dialect and fail closed on errors."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch as mock

from helpers import REPO
from honey.rsi import patch, store


class Evaluation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.env = mock.dict(os.environ, HONEY_HOME=self.root)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_generated_regex_runs_in_javascript(self):
        case = patch.write_case(self.root, 'grill', {'prompt': 'Fix pagination', 'should_fire': False,
                                                    'reply_regex': '(?i)(boundary|last page)'})
        body = Path(self.root, 'evals', case, 'graders', 'reply.md').read_text(encoding="utf-8")
        pattern = json.loads(next(l[9:] for l in body.splitlines() if l.startswith('pattern: ')))
        result = subprocess.run(['node', '-e', 'process.exit(new RegExp(process.argv[1], "i").test("BOUNDARY") ? 0 : 1)', pattern], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bad_regex_is_not_silently_omitted(self):
        with self.assertRaises(ValueError):
            patch.write_case(self.root, 'debug', {'prompt': 'fix it', 'should_fire': True, 'reply_regex': '['})
        self.assertFalse(list(Path(self.root).glob('evals/*')))

    def eval_result(self, document, code=0):
        out = str(Path(self.root, 'result.json'))
        def execute(*args, **kwargs):
            store.write_json(out, document)
            return subprocess.CompletedProcess(args[0], code, '', 'provider failed' if code > 1 else '')
        with mock.object(patch.subprocess, 'run', side_effect=execute):
            return patch.run_eval(self.root, '*', out)

    def test_grader_error_is_not_a_behavior_score(self):
        document = {'cases': [{'name': 'case', 'arms': {'with': [{'score': 0.5, 'graders': [
            {'explanation': 'grader threw: Invalid regular expression'}]}]}}]}
        with self.assertRaisesRegex(ValueError, 'grader'):
            self.eval_result(document)

    def test_incomplete_or_failed_evaluation_is_rejected(self):
        valid = {'cases': [{'name': 'case', 'arms': {'with': [{'score': 0}]}}]}
        for document, code in [({}, 0), (dict(valid, partial=True), 2), (valid, 3),
                               ({'cases': [{'name': 'case', 'arms': {'with': [{'score': None}]}}]}, 0)]:
            with self.subTest(document=document, code=code), self.assertRaises((ValueError, RuntimeError)):
                self.eval_result(document, code)

    def test_real_behavior_failure_remains_a_valid_baseline(self):
        self.assertEqual(self.eval_result({'cases': [{'name': 'case', 'arms': {'with': [{'score': 0}]}}]}, 1), {'case': 0})

    def test_missing_selected_case_is_not_a_complete_evaluation(self):
        for name in ('case', 'missing'):
            directory = Path(self.root, 'evals', name)
            directory.mkdir(parents=True)
            (directory / 'prompt.md').write_text('A maintained behavior case.\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, 'missing|coverage'):
            self.eval_result({'cases': [{'name': 'case', 'arms': {'with': [{'score': 1}]}}]})

    def test_maintained_runner_uses_scaffold_and_separate_results(self):
        out = str(Path(self.root, 'result.json'))
        def execute(command, **kwargs):
            self.assertEqual(command[command.index('--eval-dir') + 1], '.suite')
            self.assertIn('--scaffold', command)
            self.assertEqual(command[command.index('--max-cost-usd') + 1], '0.35')
            self.assertFalse(command[command.index('--output-dir') + 1].startswith(str(Path(self.root, '.suite'))))
            store.write_json(out, {'costUsd': 0, 'cases': [{'name': 'case', 'arms': {'with': [{'score': 1}]}}]})
            return subprocess.CompletedProcess(command, 0, '', '')
        with mock.object(patch.subprocess, 'run', side_effect=execute):
            self.assertEqual(patch.run_eval(self.root, '*', out, '.suite'), {'case': 1})

    def test_evaluation_rechecks_budget_after_previous_call(self):
        from honey.rsi import text
        store.append_jsonl(str(Path(self.root, 'costs.jsonl')), {'ts': text.now(), 'usd': 1.4})
        with mock.object(patch.subprocess, 'run') as provider:
            with self.assertRaisesRegex(RuntimeError, 'budget'):
                patch.run_eval(self.root, '*', str(Path(self.root, 'out.json')))
        provider.assert_not_called()

    def test_paid_timeout_retains_conservative_cost(self):
        with mock.object(patch.subprocess, 'run', side_effect=subprocess.TimeoutExpired('claude', 900)):
            with self.assertRaises(subprocess.TimeoutExpired):
                patch.run_eval(self.root, '*', str(Path(self.root, 'out.json')))
        costs = store.read_jsonl(str(Path(self.root, 'costs.jsonl')))
        self.assertEqual(sum(c.get('usd') or 0 for c in costs), 0.35)
        self.assertFalse(costs[-1]['ok'])


if __name__ == '__main__':
    unittest.main()
