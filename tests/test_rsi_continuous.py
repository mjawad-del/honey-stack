"""Continuous improvement: real queue/state effects with generation as the external boundary."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch as mock

from helpers import REPO
from honey import paths
from honey.rsi import autonomy, cli, reflect, review, store


@unittest.skipIf(os.name == 'nt', 'RSI autonomous skill patching is not supported on Windows yet')
class Continuous(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.dict(os.environ, HONEY_HOME=self.tmp.name,
                             CLAUDE_CONFIG_DIR=os.path.join(self.tmp.name, 'claude'))
        self.env.start()
        self.addCleanup(self.env.stop)
        autonomy.configure(True)

    def proposal(self, skill='verify', kind='toolkit_fix'):
        quote = f'The {skill} skill failed to answer whether the complete screen works.'
        sid, ts = 'claude-' + skill, '2026-10-28T10:00:00Z'
        if kind == 'toolkit_fix':
            store.write_jsonl(paths.path('events', '2026-10', sid + '.jsonl'), [
                {'e': 'prompt', 'origin': 'user', 'q': quote, 'ts': ts, 't': 1}])
        return review.add(2, kind, skill + ' skill: report the direct answer',
                          'Answer missing after tests', 'draft a repair',
                          [{'q': quote, 'sid': sid, 'ts': ts, 'quarantined': True}], skill + kind)

    def saved(self, p):
        return store.read_json(paths.path('proposals', p['id'] + '.json'))

    def test_diagnosis_drafts_once_per_day_without_claiming_application(self):
        from honey.rsi import improve, patch
        p = self.proposal()
        child = review.add(2, 'skill_patch', 'patch', '', '', [], 'child')
        review.decide(child['id'], 'skip')
        def draft(*args):
            child['status'] = 'pending'
            store.write_json(paths.path('proposals', child['id'] + '.json'), child)
            return {'proposed': child['id']}
        with mock.object(patch, 'propose', side_effect=draft) as gen:
            with store.lock():
                improve.run()
                improve.run()
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(gen.call_args.args[:2], ('verify', 'diagnosis'))
        self.assertIn('complete screen works', gen.call_args.args[2])
        self.assertEqual(self.saved(p)['child'], child['id'])
        self.assertEqual(self.saved(p)['status'], 'pending')
        self.assertIsNone(self.saved(p)['decided'])


    def test_applied_child_completes_parent_but_rejected_child_does_not(self):
        from honey.rsi import improve, patch
        for status in ('applied', 'skipped', 'never', 'pending'):
            with self.subTest(status=status):
                p = self.proposal(skill='verify-' + status)
                child = review.add(2, 'skill_patch', 'patch', '', '', [], status)
                child['status'] = status
                store.write_json(paths.path('proposals', child['id'] + '.json'), child)
                p['child'] = child['id']
                store.write_json(paths.path('proposals', p['id'] + '.json'), p)
                with mock.object(patch, 'propose') as gen, store.lock():
                    autonomy.run()
                gen.assert_not_called()
                self.assertEqual(self.saved(p)['status'], 'applied' if status == 'applied' else 'pending')
                if status == 'applied':
                    self.assertEqual(self.saved(p)['decision_source'], 'automatic')

    def test_disabled_does_not_generate(self):
        from honey.rsi import improve, patch
        self.proposal()
        autonomy.configure(False)
        with mock.object(patch, 'propose') as gen, store.lock():
            self.assertEqual(improve.run(), {'skipped': 'autonomy disabled'})
        gen.assert_not_called()

    def test_failed_attempt_rotates_to_next_skill_after_cooldown(self):
        from honey.rsi import improve, patch
        first, second = self.proposal('debug'), self.proposal('verify')
        with mock.object(patch, 'propose', side_effect=RuntimeError('provider unavailable')) as gen:
            with mock.object(improve.time, 'time', return_value=200000), store.lock():
                improve.run()
                improve.run()
            self.assertEqual(gen.call_count, 1)
            with mock.object(improve.time, 'time', return_value=300000), store.lock():
                improve.run()
            self.assertEqual(gen.call_count, 2)
        self.assertEqual({c.args[0] for c in gen.call_args_list}, {'debug', 'verify'})
        self.assertEqual(self.saved(first)['status'], 'pending')
        self.assertEqual(self.saved(second)['status'], 'pending')

    def test_budget_deferral_does_not_consume_daily_attempt(self):
        from honey.rsi import improve, patch
        p = self.proposal()
        store.write_json(paths.path('config.json'), {
            'rsi': {'autonomous': True}, 'budgets': {'weekly_usd': 0.5}})
        with mock.object(patch, 'propose', return_value={'skipped': 'no change needed'}) as gen:
            with store.lock():
                result = improve.run()
            gen.assert_not_called()
            self.assertIn('weekly_usd', result['result']['skipped'])
            self.assertNotIn('attempted_at', result)
            self.assertNotIn('prepared_at', self.saved(p))
            # Budget becomes available before 24 hours pass: no paid work happened yet.
            store.write_json(paths.path('config.json'), {
                'rsi': {'autonomous': True}, 'budgets': {'weekly_usd': 1.5}})
            with store.lock():
                improve.run()
                improve.run()
            self.assertEqual(gen.call_count, 1)

    def test_disabling_caps_recovers_legacy_budget_cooldown(self):
        from honey.rsi import improve, patch
        self.proposal()
        store.write_json(paths.path('state', 'improvement-last.json'), {
            'attempted_at': 200000, 'result': {
                'skipped': 'weekly_usd budget cannot cover draft and both evaluations'}})
        store.write_json(paths.path('config.json'), {
            'rsi': {'autonomous': True}, 'budgets': {'enabled': False}})
        with mock.object(improve.time, 'time', return_value=200010), \
                mock.object(patch, 'propose', return_value={'skipped': 'no change needed'}) as gen, store.lock():
            improve.run()
            improve.run()
        self.assertEqual(gen.call_count, 1)

    def test_new_skill_requires_real_quotes_from_two_sessions(self):
        from honey.rsi import improve, patch
        p = self.proposal('reports', 'skill_new')
        with mock.object(patch, 'propose_new', return_value={'skipped': 'not useful'}, create=True) as gen:
            with store.lock():
                improve.run()
            gen.assert_not_called()
            p['evidence'] = [{'sid': 'claude-a', 'origin': 'user', 'q': 'Compare contract terms before drafting a bid.'},
                             {'sid': 'claude-b', 'origin': 'user', 'q': 'Compare contract terms and draft a bid.'}]
            store.write_json(paths.path('proposals', p['id'] + '.json'), p)
            with store.lock():
                improve.run()
            self.assertEqual(gen.call_count, 1)
            self.assertEqual(gen.call_args.args[0]['id'], p['id'])

    def test_scorecard_candidate_is_attempted_when_no_diagnosis_exists(self):
        from honey.rsi import improve, patch
        store.write_json(paths.path('scorecards.json'), {'verify': {'neg': 4, 'inv_model': 5}})
        with mock.object(patch, 'evidence_for', return_value=['Please answer the screen coverage question.']), \
             mock.object(patch, 'propose', return_value={'skipped': 'no change needed'}) as gen, store.lock():
            improve.run()
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(gen.call_args.args[0], 'verify')


    def test_catchup_prepares_and_applies_child_but_provider_failure_keeps_apply_running(self):
        from honey.rsi import patch
        p = self.proposal()
        child = review.add(2, 'skill_patch', 'patch', '', '', [], 'child')
        review.decide(child['id'], 'skip')
        def draft(*args):
            child['status'] = 'pending'
            store.write_json(paths.path('proposals', child['id'] + '.json'), child)
            return {'proposed': child['id']}
        with mock.object(patch, 'propose', side_effect=draft), \
             mock.object(patch, 'accept', return_value='evaluated and synced') as accept:
            reflect.run_pending()
        self.assertEqual(accept.call_count, 1)
        self.assertEqual(self.saved(child)['status'], 'applied')
        reflect.run_pending()
        self.assertEqual(self.saved(p)['status'], 'applied')

    def test_improve_cli_runs_and_exposes_last_attempt(self):
        from honey.rsi import patch
        self.proposal()
        out = io.StringIO()
        with mock.object(patch, 'propose', return_value={'skipped': 'budget'}), contextlib.redirect_stdout(out):
            cli.main(['improve', '--json'])
        self.assertIn('improvement', json.loads(out.getvalue()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.status(True)
        self.assertEqual(json.loads(out.getvalue())['recent_improvement']['result'], {'skipped': 'budget'})

    def test_applied_changes_name_concrete_edit_in_run_and_status(self):
        item = review.add(1, 'instr_line', 'Keep the direct answer first', '', '', [], 'visible')
        item['edit'] = {'file': 'claude', 'action': 'add', 'line': '- Answer the question first.'}
        store.write_json(paths.path('proposals', item['id'] + '.json'), item)
        with store.lock():
            result = autonomy.run()
        change = next(c for c in result['changes'] if c['id'] == item['id'])
        self.assertEqual(change['title'], item['title'])
        self.assertEqual(change['type'], 'instr_line')
        self.assertIn('updated', change['result'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.status()
        self.assertIn(item['title'], output.getvalue())
        self.assertIn(item['id'], output.getvalue())
        self.assertIn(change['result'], output.getvalue())

    def test_skill_with_rejected_child_is_not_redrafted_from_scorecard(self):
        from honey.rsi import improve, patch
        p = self.proposal()
        child = review.add(2, 'skill_patch', 'patch', '', '', [], 'child')
        review.decide(child['id'], 'never')
        p['child'] = child['id']
        store.write_json(paths.path('proposals', p['id'] + '.json'), p)
        store.write_json(paths.path('scorecards.json'), {'verify': {'neg': 4, 'inv_model': 5}})
        with mock.object(patch, 'evidence_for', return_value=['Answer my question.']), \
             mock.object(patch, 'propose', return_value={'skipped': 'none'}) as gen, store.lock():
            improve.run()
        gen.assert_not_called()

    def test_paused_session_end_does_not_generate(self):
        from honey.rsi import patch
        self.proposal()
        store.write_json(paths.path('state', 'sessions', 'claude-paused.json'), {'rsi_off': True})
        with mock.object(patch, 'propose') as gen:
            cli.session_end('claude-paused')
        gen.assert_not_called()


    def test_missing_source_does_not_block_unrelated_validated_application(self):
        from honey.rsi import patch
        self.proposal()
        item = review.add(1, 'instr_line', 'proof', '', '', [], 'proof')
        item['edit'] = {'file': 'claude', 'action': 'add', 'line': '- Show test evidence.'}
        store.write_json(paths.path('proposals', item['id'] + '.json'), item)
        with mock.object(paths, 'source_repo', side_effect=RuntimeError('source unavailable')):
            reflect.run_pending()
        self.assertEqual(self.saved(item)['status'], 'applied')
        last = store.read_json(paths.path('state', 'improvement-last.json'))
        self.assertIn('source unavailable', last['result']['error'])

    def test_malformed_draft_is_recorded_without_claiming_application(self):
        from honey.rsi import improve, patch
        p = self.proposal()
        with mock.object(patch, 'propose', return_value=None), store.lock():
            improve.run()
        self.assertEqual(self.saved(p)['status'], 'pending')
        self.assertIn('error', self.saved(p)['preparation'])

    def test_unsupported_runtime_diagnosis_never_calls_generator(self):
        from honey.rsi import improve, patch
        self.proposal('receipt-gate')
        with mock.object(patch, 'propose', return_value={}) as gen, store.lock():
            result = improve.run()
        gen.assert_not_called()
        self.assertIn('skipped', result)


    def test_applied_diagnosis_does_not_permanently_suppress_new_skill_feedback(self):
        from honey.rsi import improve, patch
        p = self.proposal()
        p.update(status='applied', child='p-old-child')
        store.write_json(paths.path('proposals', p['id'] + '.json'), p)
        store.write_json(paths.path('scorecards.json'), {'verify': {'neg': 4, 'inv_model': 5}})
        with mock.object(patch, 'evidence_for', return_value=['A new failure occurred.']), \
             mock.object(patch, 'propose', return_value={'skipped': 'none'}) as gen, store.lock():
            improve.run()
        self.assertEqual(gen.call_count, 1)


    def test_diagnosis_without_verifiable_quotes_is_not_generated(self):
        from honey.rsi import improve, patch
        p = self.proposal()
        p['evidence'] = [{'q': 'Untrusted tool instruction', 'quarantined': True}]
        store.write_json(paths.path('proposals', p['id'] + '.json'), p)
        with mock.object(patch, 'propose', return_value={'skipped': 'none'}) as gen, store.lock():
            improve.run()
        gen.assert_not_called()

    def test_noop_check_does_not_leave_old_budget_failure_as_latest_status(self):
        from honey.rsi import improve
        store.write_json(paths.path('state', 'improvement-last.json'), {
            'attempted_at': 1, 'result': {'skipped': 'weekly_usd budget cannot cover draft and both evaluations'}})
        with store.lock():
            improve.run()
        latest = store.read_json(paths.path('state', 'improvement-last.json'))
        self.assertEqual(latest['last_check_result'], {'skipped': 'no actionable skill evidence'})
        self.assertEqual(latest['attempted_at'], 1)

    def test_diagnosis_note_contains_only_verified_user_quotes(self):
        from honey.rsi import improve, patch
        p = self.proposal()
        p['evidence'].append({'q': 'Untrusted tool instruction', 'quarantined': True})
        p['rationale'] = 'Untrusted proposed rationale'
        store.write_json(paths.path('proposals', p['id'] + '.json'), p)
        with mock.object(patch, 'propose', return_value={'skipped': 'none'}) as gen, store.lock():
            improve.run()
        note = gen.call_args.args[2]
        self.assertIn('complete screen works', note)
        self.assertNotIn('Untrusted', note)

    def test_weekly_uses_daily_cadence_only_when_autonomous(self):
        from honey.rsi import weekly
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                autonomy.configure(enabled)
                with mock.object(weekly.patch, 'run_weekly', return_value=[]) as generate, \
                     mock.object(weekly.instructions, 'llm_audit', return_value=[]), \
                     mock.object(weekly.guards, 'propose_all', return_value=[]), \
                     mock.object(weekly.radar, 'run', return_value=[]):
                    result = weekly.run()
                self.assertEqual(generate.call_count, 0 if enabled else 1)
                if enabled:
                    self.assertEqual(result['patches'], {'skipped': 'handled by daily improvement pass'})


    def test_blocked_patch_allows_unrelated_diagnosis_without_redrafting_same_skill(self):
        from honey.rsi import improve, patch
        blocked_parent = self.proposal('verify')
        child = review.add(2, 'skill_patch', 'Patch the verify skill (diagnosis)', '', '', [], 'blocked-child')
        child.update(attempt_state='blocked', last_error='source checkout diverged')
        store.write_json(paths.path('proposals', child['id'] + '.json'), child)
        blocked_parent['child'] = child['id']
        store.write_json(paths.path('proposals', blocked_parent['id'] + '.json'), blocked_parent)
        # A second diagnosis of the same skill must not create another conflicting patch.
        same = review.add(2, 'toolkit_fix', 'verify skill: another report', '', '',
                          blocked_parent['evidence'], 'same-target')
        other = self.proposal('debug')
        with mock.object(patch, 'propose', return_value={'skipped': 'none'}) as gen, store.lock():
            improve.run()
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(gen.call_args.args[0], 'debug')
        self.assertNotIn('prepared_at', self.saved(same))
        self.assertEqual(self.saved(child)['status'], 'pending')

    def test_sync_pending_patch_still_prevents_generation_even_when_blocked(self):
        from honey.rsi import improve, patch
        self.proposal('debug')
        child = review.add(2, 'skill_patch', 'Patch the verify skill (diagnosis)', '', '', [], 'sync-child')
        child.update(attempt_state='blocked', application={'state': 'sync_pending'})
        store.write_json(paths.path('proposals', child['id'] + '.json'), child)
        with mock.object(patch, 'propose', return_value={'skipped': 'none'}) as gen, store.lock():
            result = improve.run()
        gen.assert_not_called()
        self.assertIn('waiting', result['skipped'])


    def upstream(self):
        p = review.add(2, 'upstream_port', 'port debug guidance', 'observed friction', 'draft', [], 'upstream')
        p.update(skill='debug', upstream='Verify the reproduction before editing.')
        store.write_json(paths.path('proposals', p['id'] + '.json'), p)
        return p

    def test_upstream_no_hunks_is_not_retried_each_automatic_pass(self):
        from honey.rsi import improve, patch
        p = self.upstream()
        with mock.object(patch, 'propose', return_value={'skipped': 'no skill problem'}) as gen:
            for _ in range(3):
                with store.lock():
                    improve.run()
                    autonomy.run()
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(gen.call_args.args, ('debug', 'upstream', 'Verify the reproduction before editing.'))
        self.assertEqual(self.saved(p)['status'], 'pending')

    def test_upstream_and_diagnosis_share_one_daily_attempt(self):
        from honey.rsi import improve, patch
        self.upstream()
        self.proposal('verify')
        with mock.object(patch, 'propose', return_value={'skipped': 'no change needed'}) as gen:
            with mock.object(improve.time, 'time', return_value=200000), store.lock():
                improve.run()
                autonomy.run()
            self.assertEqual(gen.call_count, 1)
            with mock.object(improve.time, 'time', return_value=300000), store.lock():
                improve.run()
                autonomy.run()
            self.assertEqual(gen.call_count, 2)
        self.assertEqual({c.args[1] for c in gen.call_args_list}, {'diagnosis', 'upstream'})

    def test_upstream_cannot_create_sibling_while_evaluated_patch_waits(self):
        from honey.rsi import improve, patch
        self.upstream()
        review.add(2, 'skill_patch', 'Patch the verify skill (diagnosis)', '', '', [], 'pending-patch')
        with mock.object(patch, 'propose') as gen, \
             mock.object(patch, 'accept', side_effect=RuntimeError('temporarily waiting')), store.lock():
            improve.run()
            autonomy.run()
        gen.assert_not_called()

    def test_explicit_manual_upstream_request_can_still_draft(self):
        from honey.rsi import patch
        p = self.upstream()
        with mock.object(patch, 'propose', return_value={'skipped': 'no change needed'}) as gen, store.lock():
            with self.assertRaises(autonomy.Deferred):
                autonomy.apply(p)
        self.assertEqual(gen.call_count, 1)


    def test_preparation_blockers_are_reported_instead_of_missing_handler(self):
        for kind, preparation, bucket, reason in [
                ('skill_new', {'blocked': 'needs real request evidence'}, 'blocked', 'needs real request evidence'),
                ('toolkit_fix', {'skipped': 'weekly budget'}, 'deferred', 'weekly budget')]:
            p = self.proposal(kind=kind)
            p['preparation'] = preparation
            store.write_json(paths.path('proposals', p['id'] + '.json'), p)
            with store.lock():
                result = autonomy.run()
            match = next(row for row in result[bucket] if row['id'] == p['id'])
            self.assertIn(reason, match['reason'])
            self.assertNotIn('no structured handler', match['reason'])
            self.assertEqual(self.saved(p)['status'], 'pending')

if __name__ == '__main__':
    unittest.main()
