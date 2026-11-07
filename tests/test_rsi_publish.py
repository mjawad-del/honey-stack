"""Real Git publication with a disposable remote; GitHub is the only API fixture."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch as mock

from helpers import REPO
from honey import paths
from honey.rsi import autonomy, cli, delivery, improve, patch, publish, review, store


class Publication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, 'source'); self.root.mkdir()
        self.remote = Path(self.tmp.name, 'remote.git')
        self.addCleanup(mock.stopall)
        mock.dict(os.environ, HONEY_HOME=str(Path(self.tmp.name, 'state'))).start()
        mock.object(paths, 'source_repo', return_value=str(self.root)).start()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.test')
        self.write('skills/debug/SKILL.md', 'Reproduce first.\n')
        self.write('honey/runtime.py', 'version = 1\n')
        self.git('add', '.'); self.git('commit', '-qm', 'baseline')
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True)
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-qu', 'origin', 'main')
        self.base = self.git('rev-parse', 'HEAD')
        self.write('skills/debug/SKILL.md', 'Reproduce and verify.\n')
        self.write('evals/private/prompt.md', 'PRIVATE CUSTOMER REQUEST\n')
        self.git('add', '.'); self.git('commit', '-qm', 'PRIVATE SESSION MESSAGE')
        self.sha = self.git('rev-parse', 'HEAD')
        self.p = {'id': 'p-test', 'type': 'skill_patch', 'skill': 'debug', 'status': 'applied',
                  'patch_sha': self.sha, 'application': {'state': 'applied'}}
        self.p['source_snapshot'] = {'target': 'skills/debug/SKILL.md',
            'sha256': patch._runtime_digest(patch._runtime_files(str(self.root)), 'skills/debug/SKILL.md')}
        self.save()
        store.write_json(paths.path('config.json'), {'rsi': {'autonomous': True, 'publish': {
            'enabled': True, 'suggestions': True, 'repository': 'example/honey', 'base': 'main'}}})
        # Keep real Git transport, replacing only the GitHub repository identity and API.
        self.real_repository = publish.repository
        mock.object(publish, 'repository', return_value='example/honey').start()
        self.prs = []
        self.issues = []
        self.issue_creates = 0
        self.creates = 0
        self.fail_create = False
        mock.object(publish, 'gh', side_effect=self.gh).start()

    def write(self, name, text):
        target = self.root / name; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def save(self):
        store.write_json(paths.path('proposals', self.p['id'] + '.json'), self.p)

    def gh(self, *args):
        if args[0] == 'api':
            if '--slurp' in args and '--jq' in args:
                raise RuntimeError('the --slurp option is not supported with --jq')
            return json.dumps([[dict(issue, html_url=issue['url']) for issue in self.issues]])
        if args[:2] == ('issue', 'list'):
            return json.dumps(self.issues)
        if args[:2] == ('issue', 'create'):
            self.issue_creates += 1
            title = args[args.index('--title') + 1]
            body = Path(args[args.index('--body-file') + 1]).read_text()
            self.issues.append({'url': 'https://github.com/example/honey/issues/' + str(self.issue_creates),
                                'title': title, 'body': body, 'state': 'open', 'state_reason': None})
            return self.issues[-1]['url']
        if args[:2] == ('pr', 'list'):
            return json.dumps(self.prs)
        if args[:2] == ('pr', 'create'):
            self.creates += 1
            if self.fail_create:
                raise RuntimeError('GitHub temporarily unavailable')
            branch = args[args.index('--head') + 1]
            sha = self.git('ls-remote', 'origin', 'refs/heads/' + branch).split()[0]
            self.prs = [{'url': 'https://github.com/example/honey/pull/1',
                         'headRefOid': sha, 'state': 'OPEN'}]
            return self.prs[0]['url']
        raise AssertionError(args)

    def test_automatic_pass_pushes_only_skill_without_private_history(self):
        self.write('notes.md', 'PRIVATE UNRELATED WORK\n')
        self.git('add', 'notes.md')
        # The integration seam represents evaluation, already required by local application.
        with mock('honey.rsi.patch._validate_eval'):
            result = autonomy.run()
        saved = store.read_json(paths.path('proposals', self.p['id'] + '.json'))
        self.assertEqual(saved['publication']['state'], 'published')
        sha = saved['publication']['sha']
        self.assertEqual(self.git('diff', '--name-only', self.base, sha), 'skills/debug/SKILL.md')
        self.assertEqual(self.git('rev-parse', sha + '^'), self.base)
        self.assertNotIn('PRIVATE', self.git('log', '--format=%B', sha))
        self.assertNotIn('evals/private', self.git('ls-tree', '-r', '--name-only', sha))
        self.assertEqual(self.git('show', sha + ':skills/debug/SKILL.md'), 'Reproduce and verify.')
        self.assertEqual(result['publication']['published'][0]['url'], self.prs[0]['url'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.sha)
        self.assertEqual(saved['status'], 'applied')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'notes.md')
        self.assertNotIn('notes.md', self.git('ls-tree', '-r', '--name-only', sha))

    def run_publish(self):
        with mock.object(patch, '_validate_eval'):
            return publish.run()

    def test_disabled_and_personal_changes_never_publish(self):
        publish.configure('off')
        self.assertEqual(self.run_publish(), {'skipped': 'publication disabled'})
        settings = store.read_json(paths.path('config.json'))
        settings['rsi']['publish']['enabled'] = True
        store.write_json(paths.path('config.json'), settings)
        for kind in ('instr_line', 'toolkit_fix', 'guard'):
            self.p['type'] = kind; self.save()
            self.assertEqual(self.run_publish(), {'published': [], 'blocked': [],
                                                 'suggestions': {'published': [], 'blocked': []}})
        publish.gh.assert_not_called()

    def test_failed_create_retries_exact_sha_without_duplicate(self):
        self.fail_create = True
        self.assertEqual(len(self.run_publish()['blocked']), 1)
        saved = store.read_json(paths.path('proposals', 'p-test.json'))
        self.assertEqual(saved['status'], 'applied')
        self.assertEqual(saved['publication']['state'], 'blocked')
        sha = saved['publication']['sha']
        self.fail_create = False
        self.assertEqual(len(self.run_publish()['published']), 1)
        self.assertEqual(store.read_json(paths.path('proposals', 'p-test.json'))['publication']['sha'], sha)
        self.run_publish()
        self.assertEqual(self.creates, 2)

    def test_create_succeeded_but_response_lost_finds_existing_pr(self):
        real = self.gh
        def lose_response(*args):
            result = real(*args)
            if args[:2] == ('pr', 'create'):
                raise RuntimeError('connection lost after create')
            return result
        with mock.object(publish, 'gh', side_effect=lose_response):
            self.assertEqual(len(self.run_publish()['blocked']), 1)
        self.assertEqual(len(self.run_publish()['published']), 1)
        self.assertEqual(self.creates, 1)

    def test_remote_branch_changes_are_preserved(self):
        self.fail_create = True
        self.run_publish()
        saved = store.read_json(paths.path('proposals', 'p-test.json'))
        branch = saved['publication']['branch']
        # Another writer advances the public branch after the first push.
        tree = self.git('rev-parse', saved['publication']['sha'] + '^{tree}')
        changed = self.git('commit-tree', tree, '-p', saved['publication']['sha'], '-m', 'Other writer')
        self.git('push', '-q', 'origin', changed + ':refs/heads/' + branch)
        self.fail_create = False
        result = self.run_publish()
        self.assertIn('refusing to overwrite', result['blocked'][0]['reason'])
        self.assertTrue(self.git('ls-remote', 'origin', 'refs/heads/' + branch).startswith(changed))
        self.assertEqual(self.creates, 1)

    def test_closed_pr_does_not_recreate_deleted_branch(self):
        self.fail_create = True
        self.run_publish()
        saved = store.read_json(paths.path('proposals', 'p-test.json'))['publication']
        self.prs = [{'url': 'https://github.com/example/honey/pull/1',
                     'headRefOid': saved['sha'], 'state': 'CLOSED'}]
        self.git('push', '-q', 'origin', ':refs/heads/' + saved['branch'])
        self.assertIn('closed', self.run_publish()['blocked'][0]['reason'])
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/' + saved['branch']), '')

    def test_rejected_push_preserves_applied_state_and_retries(self):
        hook = self.remote / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n'); hook.chmod(0o755)
        self.assertEqual(len(self.run_publish()['blocked']), 1)
        saved = store.read_json(paths.path('proposals', 'p-test.json'))
        self.assertEqual(saved['status'], 'applied')
        self.assertEqual(self.creates, 0)
        hook.unlink()
        self.assertEqual(len(self.run_publish()['published']), 1)
        self.assertEqual(self.prs[0]['headRefOid'], saved['publication']['sha'])

    def test_dirty_runtime_or_changed_origin_blocks_before_push(self):
        self.write('honey/runtime.py', 'private_change = True\n')
        self.assertEqual(len(self.run_publish()['blocked']), 1)
        self.assertEqual(self.git('ls-remote', '--heads', 'origin', 'refs/heads/rsi/*'), '')
        self.git('restore', 'honey/runtime.py')
        with mock.object(publish, 'repository', return_value='other/repo'):
            self.assertIn('origin changed', self.run_publish()['blocked'][0]['reason'])

    def test_unpublished_runtime_ancestry_is_not_exported(self):
        self.git('checkout', '-qb', 'private-base', self.base)
        self.write('honey/runtime.py', 'private_runtime = True\n')
        self.git('commit', '-qam', 'PRIVATE HISTORY')
        self.write('skills/debug/SKILL.md', 'Reproduce and verify.\n')
        self.git('commit', '-qam', 'new evaluated patch')
        self.p['patch_sha'] = self.git('rev-parse', 'HEAD')
        self.p['source_snapshot']['sha256'] = patch._runtime_digest(
            patch._runtime_files(str(self.root)), 'skills/debug/SKILL.md')
        self.save()
        self.assertIn('baseline', self.run_publish()['blocked'][0]['reason'])
        self.assertEqual(self.git('ls-remote', '--heads', 'origin', 'refs/heads/rsi/*'), '')

    def test_missing_or_failed_evaluation_and_sync_are_not_published(self):
        self.p['application']['state'] = 'sync_pending'; self.save()
        self.assertIn('local sync', self.run_publish()['blocked'][0]['reason'])
        self.p['application']['state'] = 'applied'; self.save()
        # Real validation rejects this fixture, which intentionally has no evaluation.
        result = publish.run()
        self.assertIn('eval evidence', result['blocked'][0]['reason'])
        self.assertEqual(self.git('ls-remote', '--heads', 'origin', 'refs/heads/rsi/*'), '')

    def test_configuration_pins_origin_and_default_branch(self):
        with mock.object(publish, 'gh', return_value=json.dumps({
                'nameWithOwner': 'example/honey', 'defaultBranchRef': {'name': 'main'}})):
            self.assertEqual(publish.configure('on'), {
                'enabled': True, 'suggestions': True, 'repository': 'example/honey', 'base': 'main'})
        self.assertEqual(publish.configure('status')['base'], 'main')
        self.assertFalse(publish.configure('off')['enabled'])

    def test_repository_accepts_only_github_origins(self):
        for url in ('https://github.com/example/honey.git', 'git@github.com:example/honey.git'):
            self.git('config', 'remote.origin.url', url)
            self.assertEqual(self.real_repository(), 'example/honey')
        self.git('config', 'remote.origin.url', 'https://example.com/example/honey.git')
        with self.assertRaises(ValueError):
            self.real_repository()
        self.git('config', 'remote.origin.url', 'https://github.com/example/honey.git')
        self.git('config', 'remote.origin.pushurl', 'https://github.com/other/repository.git')
        with self.assertRaisesRegex(ValueError, 'push destination'):
            self.real_repository()

    def test_pending_suggestion_is_published_without_private_evidence(self):
        suggestion = review.add(1, 'toolkit_fix', 'ship skill: distinguish questions from permission',
                                'Answer feasibility questions before taking action.', 'Investigate the routing.',
                                [{'q': 'PRIVATE USER QUOTE', 'sid': 'PRIVATE SESSION'}], 'suggestion')
        result = self.run_publish()
        self.assertEqual(len(result['suggestions']['published']), 1)
        self.assertEqual(self.issue_creates, 1)
        self.assertIn('Answer feasibility questions', self.issues[0]['body'])
        self.assertNotIn('PRIVATE', json.dumps(self.issues))
        saved = store.read_json(paths.path('proposals', suggestion['id'] + '.json'))
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(saved['github_issue']['state'], 'published')
        self.run_publish()
        self.assertEqual(self.issue_creates, 1)

    def test_new_skill_issue_title_omits_prompt_keywords(self):
        review.add(2, 'skill_new', 'New skill for repeated ask: acme deploy payroll staging',
                   '3 asks across 2 sessions with no skill invoked.', 'Draft it with the write-skill skill.',
                   [{'q': 'deploy acme payroll to staging'}], 'cluster:acme,deploy,payroll,staging')
        self.run_publish()
        self.assertEqual(self.issue_creates, 1)
        issue = json.dumps(self.issues).lower()
        for word in ('acme', 'payroll', 'staging'):
            self.assertNotIn(word, issue)

    def suggestion(self, kind='toolkit_fix'):
        return review.add(1, kind, 'workflow suggestion', 'Review routing.', 'Add a focused regression test.',
                          [{'q': 'PRIVATE QUOTE'}], kind)

    def test_closed_published_issue_stops_autorepair_without_claiming_applied(self):
        proposal = self.suggestion()
        self.run_publish()
        self.issues[0].update(state='closed', state_reason='not_planned')
        with mock.object(autonomy, 'apply', return_value='unexpected repair') as apply, mock.object(patch, '_validate_eval'):
            autonomy.run()
        saved = store.read_json(paths.path('proposals', proposal['id'] + '.json'))
        self.assertEqual(saved['status'], 'closed')
        self.assertEqual(saved['github_issue']['state_reason'], 'not_planned')
        self.assertEqual(saved['decision_source'], 'github_issue')
        self.assertNotIn(proposal['id'], [p['id'] for p in review.pending()])
        apply.assert_not_called()
        self.assertEqual(self.issue_creates, 1)

    def test_closed_issue_is_reconciled_before_daily_generation_and_not_recreated(self):
        proposal = self.suggestion()
        proposal['title'] = 'debug skill: diagnosed workflow'
        publish.save(proposal)
        self.run_publish()
        self.issues[0].update(state='closed', state_reason='completed')
        with mock.object(patch, 'evidence_for', return_value=[{'q': 'verified'}]), \
                mock.object(patch, 'candidates', return_value=[('debug', {})]), \
                mock.object(patch, 'propose', return_value={'proposed': 'unexpected'}) as generate:
            improve.run()
        generate.assert_not_called()
        saved = store.read_json(paths.path('proposals', proposal['id'] + '.json'))
        self.assertEqual(saved['status'], 'closed')
        self.assertEqual(saved['github_issue']['state_reason'], 'completed')
        self.assertEqual(self.suggestion()['id'], proposal['id'])

    def test_verified_reopen_restores_only_remotely_closed_work(self):
        proposal = self.suggestion()
        self.run_publish()
        self.issues[0].update(state='closed', state_reason='not_planned')
        self.run_publish()
        self.assertEqual(review.pending(), [])
        self.issues[0].update(state='open', state_reason='reopened')
        self.run_publish()
        self.assertEqual([p['id'] for p in review.pending()], [proposal['id']])
        review.decide(proposal['id'], 'never')
        self.run_publish()
        self.assertEqual(review.pending(), [])

    def test_reconciliation_failure_preserves_status_and_defers_automatic_repair(self):
        proposal = self.suggestion()
        self.run_publish()
        with mock.object(publish, 'gh', side_effect=RuntimeError('authentication unavailable')), \
                mock.object(autonomy, 'apply', return_value='unexpected repair') as apply:
            result = autonomy.run()
        saved = store.read_json(paths.path('proposals', proposal['id'] + '.json'))
        self.assertEqual(saved['status'], 'pending')
        self.assertIn('authentication unavailable', saved['github_issue']['reconciliation_error'])
        self.assertEqual(result['applied'], [])
        apply.assert_not_called()

    def test_closed_parent_blocks_child_application_publication_and_delivery(self):
        parent = self.suggestion()
        self.run_publish()
        parent = store.read_json(paths.path('proposals', parent['id'] + '.json'))
        parent['child'] = self.p['id']
        publish.save(parent)
        self.issues[0].update(state='closed', state_reason='not_planned')
        publish.reconcile()
        self.p['status'] = 'pending'
        self.save()
        with mock.object(autonomy, 'apply', return_value='unexpected repair') as apply:
            result = autonomy.run()
        apply.assert_not_called()
        self.assertEqual(result['applied'], [])
        self.p['status'] = 'applied'
        self.p.pop('publication', None)
        self.save()
        with mock.object(publish, 'prepare', side_effect=RuntimeError('unexpected preparation')) as prepare:
            result = publish.run()
        prepare.assert_not_called()
        self.assertIn('closed', result['blocked'][0]['reason'])
        self.p['publication'] = {'state': 'published'}
        self.save()
        with mock.object(delivery, 'merge', side_effect=RuntimeError('unexpected merge')) as merge, \
                mock.object(delivery, 'install_release') as install:
            result = delivery.run(dict(publish.config(), auto_merge=True))
        merge.assert_not_called()
        install.assert_not_called()
        self.assertIn('closed', result['blocked'][0]['reason'])

    def test_missing_or_mismatched_issue_never_closes_local_proposal(self):
        proposal = self.suggestion()
        self.run_publish()
        original = dict(self.issues[0], state='closed', state_reason='not_planned')
        cases = [[], [dict(original, url='https://github.com/other/repo/issues/1')],
                 [dict(original, title='Unrelated issue')], [dict(original, state='unknown')],
                 [dict(original, pull_request={})]]
        for issues in cases:
            with self.subTest(issues=issues):
                self.issues = issues
                result = publish.reconcile()
                saved = store.read_json(paths.path('proposals', proposal['id'] + '.json'))
                self.assertEqual(saved['status'], 'pending')
                self.assertEqual(len(result['blocked']), 1)
        self.issues = [dict(original, state='open', state_reason=None)]
        publish.reconcile()
        saved = store.read_json(paths.path('proposals', proposal['id'] + '.json'))
        self.assertNotIn('reconciliation_error', saved['github_issue'])

    def test_status_exposes_reconciliation_without_network_or_state_changes(self):
        proposal = self.suggestion()
        self.run_publish()
        self.issues[0].update(state='closed', state_reason='not_planned')
        publish.reconcile()
        before = Path(paths.path('proposals', proposal['id'] + '.json')).read_bytes()
        output = io.StringIO()
        with mock.object(publish, 'gh') as gh, contextlib.redirect_stdout(output):
            cli.status(as_json=True)
        gh.assert_not_called()
        self.assertEqual(Path(paths.path('proposals', proposal['id'] + '.json')).read_bytes(), before)
        issue = json.loads(output.getvalue())['suggestion_issues'][0]
        self.assertEqual(issue['status'], 'closed')
        self.assertEqual(issue['state_reason'], 'not_planned')

    def test_closed_child_does_not_starve_unrelated_daily_generation(self):
        parent = self.suggestion()
        parent.update(title='debug skill: closed diagnosis', child=self.p['id'])
        publish.save(parent)
        self.run_publish()
        self.issues[0].update(state='closed', state_reason='not_planned')
        self.p['status'] = 'pending'
        self.save()
        with mock.object(patch, 'evidence_for', return_value=[{'q': 'verified'}]), \
                mock.object(patch, 'candidates', return_value=[('review', {})]), \
                mock.object(patch, 'propose', return_value={'proposed': 'new-review'}) as generate:
            improve.run()
        generate.assert_called_once_with('review', 'scorecard')

    def test_automatically_applied_parent_still_blocks_delivery_when_issue_closes(self):
        parent = self.suggestion()
        self.run_publish()
        parent = store.read_json(paths.path('proposals', parent['id'] + '.json'))
        parent.update(status='applied', decision_source='automatic', child=self.p['id'],
                      title='debug skill: closed diagnosis')
        publish.save(parent)
        self.issues[0].update(state='closed', state_reason='not_planned')
        publish.reconcile()
        saved = store.read_json(paths.path('proposals', parent['id'] + '.json'))
        self.assertEqual(saved['status'], 'applied')
        self.assertEqual(saved['github_issue'].get('remote_state'), 'closed')
        with mock.object(patch, 'evidence_for', return_value=[{'q': 'verified'}]), \
                mock.object(patch, 'candidates', return_value=[('debug', {})]), \
                mock.object(patch, 'propose') as generate:
            improve.run()
        generate.assert_not_called()
        with mock.object(delivery, 'merge', side_effect=RuntimeError('unexpected merge')) as merge:
            result = delivery.run(dict(publish.config(), auto_merge=True))
        merge.assert_not_called()
        self.assertIn('closed', result['blocked'][0]['reason'])

    def test_suggestions_publish_when_local_auto_application_is_off(self):
        self.suggestion()
        autonomy.configure(False)
        result = autonomy.run()
        self.assertEqual(len(result['publication']['suggestions']['published']), 1)
        self.assertEqual(self.creates, 0)

    def test_suggestion_is_published_before_automatic_application(self):
        suggestion = self.suggestion()
        def apply(proposal):
            saved = store.read_json(paths.path('proposals', proposal['id'] + '.json'))
            self.assertEqual(saved['github_issue']['state'], 'published')
            return 'applied fixture'
        with mock.object(autonomy, 'apply', side_effect=apply), mock.object(patch, '_validate_eval'):
            result = autonomy.run()
        self.assertEqual(len(result['publication']['suggestions']['published']), 1)
        saved = store.read_json(paths.path('proposals', suggestion['id'] + '.json'))
        self.assertEqual(saved['status'], 'applied')

    def test_issue_creation_response_loss_reuses_existing_issue(self):
        suggestion = self.suggestion()
        real = self.gh
        def lose_response(*args):
            result = real(*args)
            if args[:2] == ('issue', 'create'):
                raise RuntimeError('response lost')
            return result
        with mock.object(publish, 'gh', side_effect=lose_response):
            self.assertEqual(len(self.run_publish()['suggestions']['blocked']), 1)
        self.assertEqual(len(self.run_publish()['suggestions']['published']), 1)
        self.assertEqual(self.issue_creates, 1)
        self.assertEqual(store.read_json(paths.path('proposals', suggestion['id'] + '.json'))['status'], 'pending')

    def test_existing_issue_is_reused_after_local_receipt_loss(self):
        suggestion = self.suggestion()
        self.run_publish()
        self.issues[0].update(state='closed', state_reason='not_planned')
        store.write_json(paths.path('proposals', suggestion['id'] + '.json'), suggestion)
        self.run_publish()
        self.assertEqual(self.issue_creates, 1)
        self.assertEqual(store.read_json(paths.path('proposals', suggestion['id'] + '.json'))['status'], 'closed')

    def test_personal_proposals_and_old_opt_in_never_create_issues(self):
        for kind in ('instr_line', 'instr_edit', 'instr_remove', 'guard', 'skill_patch'):
            self.suggestion(kind)
        self.assertEqual(self.run_publish()['suggestions']['published'], [])
        self.suggestion()
        settings = store.read_json(paths.path('config.json'))
        settings['rsi']['publish'].pop('suggestions')
        store.write_json(paths.path('config.json'), settings)
        self.assertEqual(self.run_publish()['suggestions']['published'], [])
        self.assertEqual(self.issue_creates, 0)

    def test_public_issue_redacts_secrets_paths_links_and_mentions(self):
        suggestion = self.suggestion()
        suggestion['rationale'] = 'Inspect someone@example.com at /Users/private/project and https://private.example @somebody'
        store.write_json(paths.path('proposals', suggestion['id'] + '.json'), suggestion)
        self.run_publish()
        body = self.issues[0]['body']
        for private in ('someone@example.com', '/Users/private', 'https://private.example', '@somebody', 'PRIVATE QUOTE'):
            self.assertNotIn(private, body)
        self.assertIn('unimplemented suggestion', body)

    def test_origin_mismatch_blocks_suggestion_without_external_writes(self):
        self.suggestion()
        with mock.object(publish, 'repository', return_value='other/repo'):
            result = self.run_publish()
        self.assertIn('origin changed', result['suggestions']['blocked'][0]['reason'])
        self.assertEqual(self.issue_creates, 0)


if __name__ == '__main__':
    unittest.main()
