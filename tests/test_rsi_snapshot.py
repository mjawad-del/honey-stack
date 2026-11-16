"""Evaluated runtime parity with real worktrees; only the evaluator/sync boundaries are fixtures."""
import os
import fnmatch
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch as mock
from helpers import REPO
from honey import paths
from honey.rsi import patch, store


class RuntimeSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, 'repo'); self.root.mkdir()
        self.addCleanup(mock.stopall)
        mock.dict(os.environ, HONEY_HOME=str(Path(self.tmp.name, 'state'))).start()
        mock.object(paths, 'source_repo', return_value=str(self.root)).start()
        for args in [('init', '-q'), ('config', 'user.name', 'Test'), ('config', 'user.email', 'test@example.test')]:
            self.git(*args)
        self.write('skills/debug/SKILL.md', 'Reproduce first.\n')
        self.write('honey/runtime.py', 'old = True\n')
        self.write('hooks/remove.py', 'old = True\n')
        for name in ['behavioral.json', *json.loads(Path(REPO, 'evals/behavioral.json').read_text(encoding="utf-8"))['cases']]:
            source, target = Path(REPO, 'evals', name), self.root / 'evals' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        self.git('add', '.'); self.git('commit', '-qm', 'initial')
        self.before = self.git('rev-parse', 'HEAD')

    def write(self, name, content):
        store.write_text(str(self.root / name), content)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True, text=True, encoding='utf-8').stdout.strip()

    def evaluate(self, root, pattern, out, eval_dir='evals'):
        changed = Path(root, 'skills/debug/SKILL.md').read_text(encoding="utf-8") == 'Reproduce and verify.\n'
        return {p.name: (1 if p.name.startswith('behavior-') else int(changed))
                for p in Path(root, eval_dir).iterdir() if p.is_dir() and fnmatch.fnmatch(p.name, pattern)}

    def draft(self, evaluator=None):
        with mock.object(patch, 'run_eval', side_effect=evaluator or self.evaluate):
            result = patch._evaluate('debug', 'diagnosis', 'Reproduce and verify.\n', 'Fix observed failure',
                                     [{'prompt': 'Debug the failure', 'should_fire': True, 'reply_regex': ''}],
                                     ['Please reproduce this failure.'])
        return store.read_json(paths.path('proposals', result['proposed'] + '.json'))

    def test_maintained_cases_are_selected_with_candidate(self):
        calls = []
        def evaluate(root, pattern, out, eval_dir='evals'):
            selected = self.evaluate(root, pattern, out, eval_dir)
            self.assertIn('behavior-preserve-readonly', selected)
            self.assertIn('behavior-scoped-config-edit', selected)
            self.assertIn('behavior-failed-verification', selected)
            calls.append(selected)
            return selected
        p = self.draft(evaluate)
        self.assertEqual(len(calls), 2)
        self.assertEqual(p['eval']['behavioral']['runner'], 'claude-plugin-eval')

    def test_changed_generated_grader_is_rejected(self):
        def evaluate(root, pattern, out, eval_dir='evals'):
            result = self.evaluate(root, pattern, out, eval_dir)
            path = next(Path(root, 'evals').glob('rsi-debug-*/graders/trigger.md'))
            path.write_text('tampered grader', encoding="utf-8")
            return result
        with self.assertRaisesRegex(ValueError, 'fixture|grader'):
            self.draft(evaluate)

    def test_changed_prepared_grader_is_rejected(self):
        def evaluate(root, pattern, out, eval_dir='evals'):
            result = self.evaluate(root, pattern, out, eval_dir)
            path = Path(root, eval_dir, 'behavior-preserve-readonly/graders/state.md')
            path.write_text('tampered grader', encoding="utf-8")
            return result
        with self.assertRaisesRegex(ValueError, 'fixture|grader'):
            self.draft(evaluate)

    def test_changed_maintained_manifest_blocks_application(self):
        p = self.draft()
        self.write('evals/behavioral.json', '{}')
        with mock('honey.install.sync', return_value=0) as sync:
            with self.assertRaisesRegex(ValueError, 'behavioral'):
                patch.accept(p)
        sync.assert_not_called()
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.before)

    def test_behavior_failure_or_missing_outcome_drops_proposal(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                def evaluate(root, pattern, out, eval_dir='evals'):
                    result = self.evaluate(root, pattern, out, eval_dir)
                    if missing:
                        result.pop('behavior-preserve-readonly')
                    else:
                        result['behavior-preserve-readonly'] = 0
                    return result
                with mock.object(patch, 'run_eval', side_effect=evaluate):
                    result = patch._evaluate('debug', 'diagnosis', 'Reproduce and verify.\n', 'Fix failure',
                                             [{'prompt': 'Debug', 'should_fire': True, 'reply_regex': ''}], [])
                self.assertIn('dropped', result)
                self.assertEqual(self.git('rev-parse', 'HEAD'), self.before)

    def test_worktree_evaluates_dirty_untracked_and_deleted_runtime_without_committing_it(self):
        self.write('honey/runtime.py', 'current = True\n')
        self.write('honey/new.py', 'new = True\n')
        (self.root / 'hooks/remove.py').unlink()
        self.write('honey/__pycache__/ignored.pyc', 'cached')
        def evaluate(root, pattern, out, eval_dir="evals"):
            self.assertEqual(Path(root, 'honey/runtime.py').read_text(encoding="utf-8"), 'current = True\n')
            self.assertEqual(Path(root, 'honey/new.py').read_text(encoding="utf-8"), 'new = True\n')
            self.assertFalse(Path(root, 'hooks/remove.py').exists())
            self.assertFalse(Path(root, 'honey/__pycache__/ignored.pyc').exists())
            return self.evaluate(root, pattern, out, eval_dir)
        p = self.draft(evaluate)
        self.assertTrue(p.get('source_snapshot'))
        self.assertEqual(self.git('show', p['branch'] + ':honey/runtime.py'), 'old = True')
        with mock('honey.install.sync', return_value=0):
            patch.accept(p)
        self.assertEqual((self.root / 'honey/runtime.py').read_text(encoding="utf-8"), 'current = True\n')
        self.assertEqual((self.root / 'honey/new.py').read_text(encoding="utf-8"), 'new = True\n')
        self.assertFalse((self.root / 'hooks/remove.py').exists())

    def test_runtime_drift_after_evaluation_blocks_application(self):
        p = self.draft()
        self.write('honey/runtime.py', 'changed_after_evaluation = True\n')
        with mock('honey.install.sync', return_value=0) as sync:
            with self.assertRaisesRegex(ValueError, 'runtime.*changed|snapshot'):
                patch.accept(p)
        sync.assert_not_called()
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.before)

    def test_runtime_drift_blocks_sync_retry(self):
        p = self.draft()
        with mock('honey.install.sync', return_value=1):
            with self.assertRaises(RuntimeError):
                patch.accept(p)
        self.write('honey/runtime.py', 'changed_after_merge = True\n')
        with mock('honey.install.sync', return_value=0) as sync:
            with self.assertRaisesRegex(ValueError, 'runtime.*changed|snapshot'):
                patch.accept(p)
        sync.assert_not_called()

    def test_legacy_proposal_blocks_dirty_runtime(self):
        p = self.draft()
        p.pop('source_snapshot', None)
        self.write('honey/runtime.py', 'dirty = True\n')
        with mock('honey.install.sync', return_value=0) as sync:
            with self.assertRaisesRegex(ValueError, 'runtime|snapshot'):
                patch.accept(p)
        sync.assert_not_called()

    def test_legacy_proposal_allows_unrelated_docs(self):
        p = self.draft()
        p.pop('source_snapshot', None)
        self.write('docs/notes.md', 'Keep my notes\n')
        with mock('honey.install.sync', return_value=0):
            patch.accept(p)
        self.assertEqual((self.root / 'docs/notes.md').read_text(encoding="utf-8"), 'Keep my notes\n')


    def test_evaluator_runtime_mutation_is_not_accepted_as_snapshot_proof(self):
        def evaluate(root, pattern, out, eval_dir="evals"):
            Path(root, 'honey/runtime.py').write_text('modified_during_eval = True\n', encoding="utf-8")
            return self.evaluate(root, pattern, out, eval_dir)
        with self.assertRaisesRegex(ValueError, 'runtime.*changed|snapshot'):
            self.draft(evaluate)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.before)
        self.assertFalse(list((self.root / '.worktrees').glob('*')))

    def test_evaluator_cannot_replace_skill_after_scoring_either_arm(self):
        for target_arm in (1, 2):
            calls = []
            def evaluate(root, pattern, out, eval_dir='evals'):
                result = self.evaluate(root, pattern, out, eval_dir)
                calls.append(root)
                if len(calls) == target_arm:
                    Path(root, 'skills/debug/SKILL.md').write_text('Untested replacement after scoring.', encoding="utf-8")
                return result
            with self.subTest(arm=target_arm), self.assertRaisesRegex(ValueError, 'target.*changed'):
                self.draft(evaluate)
            self.assertEqual(self.git('rev-parse', 'HEAD'), self.before)


if __name__ == '__main__': unittest.main()
