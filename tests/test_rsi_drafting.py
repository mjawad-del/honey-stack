"""Automatic skill drafts retain real git/evidence effects; providers are fixtures."""
import os
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch as mock
from helpers import REPO
from honey import paths
from honey.rsi import patch, store, text

@unittest.skipIf(os.name == 'nt', 'RSI autonomous skill patching is not supported on Windows yet')
class Drafting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, 'repo'); self.root.mkdir()
        self.addCleanup(mock.stopall)
        mock.dict(os.environ, HONEY_HOME=str(Path(self.tmp.name, 'state'))).start()
        mock.object(paths, 'source_repo', return_value=str(self.root)).start()
        for args in [('init', '-q'), ('config', 'user.name', 'Test'), ('config', 'user.email', 'test@example.test')]:
            self.git(*args)
        store.write_text(str(self.root / 'skills/debug/SKILL.md'), '---\nname: debug\ndescription: Debug failures\n---\nReproduce first.\n')
        for name in ['behavioral.json', *json.loads(Path(REPO, 'evals/behavioral.json').read_text(encoding="utf-8"))['cases']]:
            source, target = Path(REPO, 'evals', name), self.root / 'evals' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        self.git('add', '.'); self.git('commit', '-qm', 'initial')
        self.proposal = {'id': 'p-new', 'type': 'skill_new', 'title': 'Repeated changelog task', 'evidence': [
            {'sid': 'claude-one', 'q': 'Write release notes from these changes', 'quarantined': False},
            {'sid': 'claude-two', 'q': 'Prepare release notes for this update', 'quarantined': False}]}
        self.draft = {'name': 'release-notes', 'description': 'Use when asked to prepare release notes.',
                      'body': '# Release notes\nRead changes and group user-visible updates. Verify claims against the diff.',
                      'rationale': 'Repeated request with no existing matching skill',
                      'cases': [{'prompt': 'Write release notes for these changes.', 'should_fire': True, 'reply_regex': ''},
                                {'prompt': 'What is two plus two?', 'should_fire': False, 'reply_regex': ''}]}

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True, text=True, encoding='utf-8').stdout.strip()

    def evaluate(self, root, pattern, out, eval_dir="evals"):
        has_skill = Path(root, 'skills/release-notes/SKILL.md').exists()
        cases = list(Path(root, eval_dir).glob('rsi-release-notes-*/graders/trigger.md'))
        scores = {p.parent.parent.name: 1 if 'max: 0' in p.read_text(encoding="utf-8") or has_skill else 0 for p in cases}
        scores.update({p.name: 1 for p in Path(root, eval_dir).glob('behavior-*')})
        return scores

    def test_new_skill_becomes_evaluated_child_without_applying_parent(self):
        with mock('honey.rsi.reflect.call_llm', return_value=(self.draft, 0, 'fake')), mock.object(patch, 'run_eval', side_effect=self.evaluate):
            result = patch.propose_new(self.proposal)
        child = store.read_json(paths.path('proposals', result['proposed'] + '.json'))
        self.assertEqual(child['type'], 'skill_patch')
        self.assertEqual(child['status'], 'pending')
        self.assertFalse((self.root / 'skills/release-notes/SKILL.md').exists())
        self.assertIn('release-notes', self.git('show', child['branch'] + ':skills/release-notes/SKILL.md'))
        self.assertEqual(len(child['eval']['after']), 5)

    def test_keyword_only_or_untrusted_requests_do_not_call_provider(self):
        for evidence in [[{'q': 'next one'}], [dict(e, quarantined=True) for e in self.proposal['evidence']]]:
            with mock('honey.rsi.reflect.call_llm') as provider:
                result = patch.propose_new(dict(self.proposal, evidence=evidence))
            self.assertIn('evidence', str(result))
            provider.assert_not_called()

    def test_entire_patch_budget_reserved_before_generation(self):
        store.append_jsonl(paths.path('costs.jsonl'), {'ts': text.now(), 'job': 'reflect', 'usd': 0.8})
        with mock('honey.rsi.reflect.call_llm') as provider:
            result = patch.propose_new(self.proposal)
        self.assertIn('budget', str(result))
        provider.assert_not_called()

    def test_rejected_draft_cleans_its_worktree_and_branch(self):
        with mock('honey.rsi.reflect.call_llm', return_value=(self.draft, 0, 'fake')), mock.object(patch, 'run_eval', side_effect=RuntimeError('provider failed')):
            with self.assertRaisesRegex(RuntimeError, 'provider failed'):
                patch.propose_new(self.proposal)
        self.assertNotIn('rsi/', self.git('branch', '--list'))
        self.assertFalse(list((self.root / '.worktrees').glob('*')))

    def test_unknown_target_and_empty_evidence_do_not_call_provider(self):
        with mock('honey.rsi.reflect.call_llm') as provider:
            with self.assertRaises(ValueError):
                patch.propose('../debug', 'scorecard')
            result = patch.propose('debug', 'scorecard')
        self.assertIn('evidence', str(result))
        provider.assert_not_called()

    def test_new_skill_cannot_overwrite_or_traverse(self):
        for name in ['debug', '../escape']:
            with mock('honey.rsi.reflect.call_llm', return_value=(dict(self.draft, name=name), 0, 'fake')):
                with self.assertRaises(ValueError):
                    patch.propose_new(self.proposal)
        self.assertIn('Reproduce first.', (self.root / 'skills/debug/SKILL.md').read_text(encoding="utf-8"))

    def test_paid_draft_failure_retains_conservative_cost(self):
        with mock('honey.rsi.reflect.call_llm', side_effect=RuntimeError('invalid provider JSON')):
            with self.assertRaises(RuntimeError):
                patch.propose_new(self.proposal)
        costs = store.read_jsonl(paths.path('costs.jsonl'))
        self.assertEqual(sum(c.get('usd') or 0 for c in costs), 0.15)
        self.assertFalse(costs[-1]['ok'])

if __name__ == '__main__': unittest.main()
