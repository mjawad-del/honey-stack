"""Apply-time evidence and conflict checks, with disposable instruction and git files."""
import os
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from honey import paths
from honey.rsi import behavioral, guards, instructions, patch, store


class ApplyValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(mock.stopall)
        mock.dict(os.environ, HONEY_HOME=self.tmp.name, CLAUDE_CONFIG_DIR=self.tmp.name).start()
        self.fp = os.path.join(self.tmp.name, 'CLAUDE.md')
        store.write_text(self.fp, '- keep this entire line\n')

    def proposal(self, **edit):
        return {'id': 'p-test', 'edit': dict(file='claude', action='add', line='test', **edit)}

    def rejects(self, fn, *args):
        with self.assertRaises((ValueError, SystemExit, RuntimeError)):
            fn(*args)

    def test_instruction_validation(self):
        for edit in ({'file': 'unknown', 'action': 'add', 'line': 'x'},
                     {'file': 'claude', 'action': 'add', 'line': 'x\ny'},
                     {'file': 'claude', 'action': 'add', 'line': ''},
                     {'file': 'claude', 'action': 'add', 'line': '- '},
                     {'file': 'claude', 'action': 'remove', 'line': 'keep this'},
                     {'file': 'claude', 'action': 'edit', 'line': '- keep this entire line', 'replacement': ''}):
            self.rejects(instructions.apply, {'id': 'p-test', 'edit': edit})
            self.assertEqual(Path(self.fp).read_text(encoding="utf-8"), '- keep this entire line\n')

    def test_instruction_missing_proposal_id_is_not_applied(self):
        p = self.proposal()
        del p['id']
        self.rejects(instructions.apply, p)
        self.assertEqual(Path(self.fp).read_text(encoding="utf-8"), '- keep this entire line\n')

    def test_revert_preserves_later_edits(self):
        p = self.proposal()
        instructions.apply(p)
        store.write_text(self.fp, Path(self.fp).read_text(encoding="utf-8") + 'later edit\n')
        self.rejects(instructions.revert, p['id'])
        self.assertIn('later edit', Path(self.fp).read_text(encoding="utf-8"))
        self.assertNotEqual(store.read_json(paths.path('proposals', p['id'] + '.json')).get('status'), 'reverted')

    def test_revert_and_heading(self):
        p = self.proposal()
        instructions.apply(p)
        self.assertNotIn('approved in', Path(self.fp).read_text(encoding="utf-8"))
        instructions.revert(p['id'])
        self.assertEqual(Path(self.fp).read_text(encoding="utf-8"), '- keep this entire line\n')

    def test_guard_validation(self):
        valid = {'id': 'g-test', 'status': 'proposed', 'regex': '^git commit', 'unless_prompt': 'commit',
                 'message': 'ask first', 'action': 'block', 'verify': {'replay_hits': 1, 'fp': 0, 'fp_n': 200}}
        for update in ({'regex': '['}, {'unless_prompt': '['}, {'regex': ''},
                       {'verify': {'replay_hits': 0, 'fp': 0, 'fp_n': 200}},
                       {'verify': {'replay_hits': 1, 'fp': 0, 'fp_n': 0}},
                       {'verify': {'replay_hits': 1, 'fp': 3, 'fp_n': 200}}):
            g = dict(valid, **update)
            guards.save([g])
            self.rejects(guards.activate, g['id'])
            self.assertEqual(guards.load(), [g])
        guards.save([valid])
        self.rejects(guards.activate, 'missing')
        guards.activate(valid['id'])
        self.assertEqual(guards.load()[0]['status'], 'active')

    def repo(self):
        repo = os.path.join(self.tmp.name, 'repo')
        os.mkdir(repo)
        def git(*args):
            return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True, text=True, encoding='utf-8').stdout.strip()
        git('init', '-q'); git('config', 'user.email', 'test@example.com'); git('config', 'user.name', 'Test')
        store.write_text(os.path.join(repo, 'skill'), 'before\n')
        fixture_root = Path(__file__).resolve().parents[1] / 'evals'
        for name in ['behavioral.json', *json.loads((fixture_root / 'behavioral.json').read_text(encoding="utf-8"))['cases']]:
            source, target = fixture_root / name, Path(repo, 'evals', name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        store.write_text(os.path.join(repo, 'evals/prior/prompt.md'), 'Neighboring skill fixture')

        git('add', '.'); git('commit', '-qm', 'before')
        git('checkout', '-qb', 'rsi/test')
        store.write_text(os.path.join(repo, 'skill'), 'after\n')
        git('commit', '-qam', 'after')
        commit = git('rev-parse', 'HEAD')
        git('checkout', '-q', '-')
        mock('honey.install.sync', return_value=0).start()
        mock.object(paths, 'source_repo', return_value=repo).start()
        real_git = patch.git
        mock.object(patch, 'git', side_effect=lambda *a, **k: real_git(*a, **dict(k, cwd=repo))).start()
        maintained = behavioral.snapshot(repo)
        digests = dict(maintained['cases'], prior=behavioral.case_digest(repo, 'prior'))
        digests['rsi-case'] = '0' * 64
        before = {name: 1 for name in digests}; before['rsi-case'] = 0
        after = {name: 1 for name in digests}
        return repo, git, {'id': 'p-patch', 'branch': 'rsi/test', 'patch_sha': commit,
                           'worktree': os.path.join(repo, 'absent'),
                           'eval': {'before': before, 'after': after, 'case': 'rsi-case',
                                    'behavioral': maintained, 'case_sha256': digests,
                                    'generated_cases': ['rsi-case']}}, commit

    def test_patch_evidence_before_merge(self):
        repo, git, p, commit = self.repo()
        start = git('rev-parse', 'HEAD')
        for evidence in ({}, {'before': {'case': 0, 'prior': 0}, 'after': {'case': 1}, 'case': 'case'},
                         {'before': {'case': 1}, 'after': {'case': 1}, 'case': 'case'},
                         {'before': {'case': 0, 'prior': 1}, 'after': {'case': 1, 'prior': 0}, 'case': 'case'}):
            self.rejects(patch.accept, dict(p, eval=evidence))
            self.assertEqual(git('rev-parse', 'HEAD'), start)

    def test_patch_missing_or_failed_maintained_evidence_is_rejected(self):
        repo, git, p, commit = self.repo()
        start = git('rev-parse', 'HEAD')
        for change in ('legacy', 'missing', 'failed'):
            candidate = json.loads(json.dumps(p))
            evidence = candidate['eval']
            if change == 'legacy':
                evidence.pop('behavioral')
            elif change == 'missing':
                evidence['after'].pop('behavior-preserve-readonly')
            else:
                evidence['after']['behavior-preserve-readonly'] = 0
            self.rejects(patch.accept, candidate)
            self.assertEqual(git('rev-parse', 'HEAD'), start)

    def test_candidate_cannot_change_maintained_grader(self):
        repo, git, p, commit = self.repo()
        start = git('rev-parse', 'HEAD')
        git('checkout', '-q', p['branch'])
        Path(repo, 'evals/behavior-preserve-readonly/graders/state.md').write_text('tampered', encoding="utf-8")
        git('commit', '-qam', 'change grader')
        p['patch_sha'] = git('rev-parse', 'HEAD')
        git('checkout', '-q', '-')
        with self.assertRaisesRegex(ValueError, 'cannot modify maintained'):
            patch.accept(p)
        self.assertEqual(git('rev-parse', 'HEAD'), start)

    def test_neighboring_case_drift_blocks_application(self):
        repo, git, p, commit = self.repo()
        start = git('rev-parse', 'HEAD')
        Path(repo, 'evals/prior/prompt.md').write_text('different behavior', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, 'fixture changed'):
            patch.accept(p)
        self.assertEqual(git('rev-parse', 'HEAD'), start)

    def test_patch_sync_failure_is_retryable(self):
        repo, git, p, commit = self.repo()
        with mock('honey.install.sync', return_value=1):
            self.rejects(patch.accept, p)
        self.assertEqual(git('rev-parse', 'HEAD'), commit)
        saved = store.read_json(paths.path('proposals', p['id'] + '.json'))
        self.assertEqual(saved['application']['state'], 'sync_pending')
        with mock('honey.install.sync', return_value=0):
            self.assertIn('synced', patch.accept(saved))
        self.assertEqual(saved['application']['state'], 'applied')
        cleanup = [c for c in patch.git.call_args_list if c.args[:2] == ('worktree', 'remove')]
        self.assertTrue(cleanup)
        self.assertNotIn('--force', cleanup[-1].args)

    def test_patch_source_ahead_is_not_evaluated(self):
        repo, git, p, commit = self.repo()
        git('merge', '--ff-only', '-q', p['branch'])
        store.write_text(os.path.join(repo, 'new'), 'new source')
        git('add', '.'); git('commit', '-qm', 'new source')
        self.rejects(patch.accept, p)

    def test_patch_changed_branch(self):
        repo, git, p, commit = self.repo()
        p['patch_sha'] = '0' * 40
        self.rejects(patch.accept, p)

    def test_instruction_exact_edit_and_remove(self):
        p = {'id': 'p-edit', 'edit': {'file': 'claude', 'action': 'edit',
             'line': '- keep this entire line', 'replacement': '- replacement'}}
        instructions.apply(p)
        self.assertEqual(Path(self.fp).read_text(encoding="utf-8"), '- replacement\n')
        p = {'id': 'p-remove', 'edit': {'file': 'claude', 'action': 'remove', 'line': '- replacement'}}
        instructions.apply(p)
        self.assertEqual(Path(self.fp).read_text(encoding="utf-8"), '')

    def test_patch_dirty_target_and_diverged(self):
        repo, git, p, commit = self.repo()
        store.write_text(os.path.join(repo, 'skill'), 'user edit')
        self.rejects(patch.accept, p)
        git('add', '.'); git('commit', '-qm', 'diverged')
        start = git('rev-parse', 'HEAD')
        self.rejects(patch.accept, p)
        self.assertEqual(git('rev-parse', 'HEAD'), start)

    def test_patch_preserves_unrelated_uncommitted_work(self):
        repo, git, p, commit = self.repo()
        store.write_text(os.path.join(repo, 'unrelated'), 'user edit')
        patch.accept(p)
        self.assertEqual(Path(repo, 'unrelated').read_text(encoding="utf-8"), 'user edit')
        self.assertEqual(Path(repo, 'skill').read_text(encoding="utf-8"), 'after\n')
        self.assertEqual(git('rev-parse', 'HEAD'), commit)

    def test_patch_cooldown_begins_after_successful_application(self):
        repo, git, p, commit = self.repo()
        git('branch', 'rsi/debug-123', p['branch'])
        p['branch'] = 'rsi/debug-123'
        with mock('honey.install.sync', return_value=1):
            self.rejects(patch.accept, p)
        self.assertFalse(store.read_json(paths.path('scorecards.json'), {}).get('debug', {}).get('patched_at'))
        patch.accept(p)
        self.assertTrue(store.read_json(paths.path('scorecards.json'))['debug']['patched_at'])


if __name__ == '__main__':
    unittest.main()
