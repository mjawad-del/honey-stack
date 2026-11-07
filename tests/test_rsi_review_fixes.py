"""Regressions for the RSI review: reflection retries, notes under lock, redaction, repair undo, instruction autonomy."""
import contextlib
import io
import os
import tempfile
import threading
import unittest
from unittest.mock import patch as mock

from helpers import REPO  # noqa: F401  (sets sys.path)
from honey import install, paths
from honey.rsi import autonomy, curate, instructions, reflect, review, store, text


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claude = os.path.join(self.tmp.name, 'claude')
        os.makedirs(self.claude)
        env = mock.dict(os.environ, HONEY_HOME=os.path.join(self.tmp.name, 'honey'), CLAUDE_CONFIG_DIR=self.claude,
                        CODEX_HOME=os.path.join(self.tmp.name, 'codex'))
        env.start()
        self.addCleanup(env.stop)


class ReflectionQueue(Isolated):
    def test_failed_calls_do_not_consume_the_daily_success_cap(self):
        for _ in range(7):
            store.append_jsonl(paths.path('costs.jsonl'), {'ts': text.now(), 'job': 'reflect', 'usd': None, 'ok': False})
        store.append_jsonl(paths.path('costs.jsonl'), {'ts': text.now(), 'job': 'reflect', 'usd': 0, 'ok': True})
        self.assertIsNone(reflect.budget_block('claude'))  # 8 calls today, only 1 successful
        for _ in range(7):
            store.append_jsonl(paths.path('costs.jsonl'), {'ts': text.now(), 'job': 'reflect', 'usd': 0, 'ok': True})
        self.assertEqual(reflect.budget_block('claude'), 'daily reflection cap')

    def test_failures_have_their_own_daily_cap(self):
        for _ in range(reflect.BUDGETS['max_failed_reflections_per_day']):
            store.append_jsonl(paths.path('costs.jsonl'), {'ts': text.now(), 'job': 'reflect', 'usd': None, 'ok': False})
        self.assertEqual(reflect.budget_block('claude'), 'daily reflection failure cap')

    def test_repeatedly_failing_session_is_retired_and_others_go_first(self):
        for key in ('claude-old', 'claude-new'):
            store.append_jsonl(paths.path('events', '2026-11', key + '.jsonl'), {'e': 'end'})
        with mock.object(reflect.digest, 'worth_reflecting', return_value=True), \
                mock.object(reflect.digest, 'build', return_value=('body', [], {})), \
                mock.object(reflect, 'reflect_call', side_effect=RuntimeError('claude -p failed')):
            for attempt in range(reflect.MAX_FAILURES):
                self.assertEqual([k for k, _ in reflect.due_sessions()][-1], 'claude-old' if attempt else 'claude-new')
                self.assertEqual(reflect.reflect_session('claude-old', reflect.digest.load_events('claude-old')), 'failed')
        self.assertEqual([k for k, _ in reflect.due_sessions()], ['claude-new'])
        store.append_jsonl(paths.path('events', '2026-11', 'claude-old.jsonl'), {'e': 'prompt'})
        self.assertIn('claude-old', [k for k, _ in reflect.due_sessions()])

    def test_failed_engine_falls_back_to_the_other_engine(self):
        calls = []

        def call(system, user, schema, max_usd, model='haiku', only=None):
            calls.append(only)
            if only == 'claude':
                raise RuntimeError('claude -p failed: credit balance')
            return {'candidates': []}, None, 'codex'

        with mock.object(reflect, 'engines', return_value=[('claude', '/c'), ('codex', '/x')]), \
                mock.object(reflect, 'call_llm', side_effect=call):
            result, usd, engine = reflect.reflect_call('system', 'body', {}, 0.15)
        self.assertEqual((engine, calls), ('codex', ['claude', 'codex']))
        failures = [c for c in store.read_jsonl(paths.path('costs.jsonl')) if c.get('ok') is False]
        self.assertEqual([c['engine'] for c in failures], ['claude'])


class NotesUnderLock(Isolated):
    def test_note_is_queued_not_lost_while_a_job_holds_the_lock(self):
        held, release = threading.Event(), threading.Event()

        def job():
            with store.lock():
                held.set()
                release.wait(10)

        worker = threading.Thread(target=job)
        worker.start()
        held.wait(5)
        try:
            with mock.object(curate, 'NOTE_LOCK_WAIT', 0.2):
                bullet, merged = curate.add_note('when shipping, run the tests first', 'global', 'claude', 's1', '')
        finally:
            release.set()
            worker.join()
        self.assertEqual(merged, 'queued')
        self.assertEqual(curate.load(), [])
        with store.lock():
            self.assertEqual(curate.drain_pending_notes(), 1)
        saved = curate.load()
        self.assertEqual([b['id'] for b in saved], [bullet['id']])
        self.assertEqual(saved[0]['status'], 'active')
        self.assertFalse(os.path.exists(paths.path('pending-notes.jsonl')))

    def test_note_with_free_lock_also_drains_earlier_queued_notes(self):
        store.append_jsonl(paths.path('pending-notes.jsonl'), curate._note_record(
            'always run the linter', 'global', 'claude', 's0', ''))
        bullet, merged = curate.add_note('when shipping, run the tests first', 'global', 'claude', 's1', '')
        self.assertIs(merged, False)
        self.assertEqual(len(curate.load()), 2)


class Redaction(unittest.TestCase):
    def test_common_secret_shapes_are_redacted(self):
        for raw, secret in (('export OPENAI_API_KEY=abcdEFGH1234ijklMNOP', 'abcdEFGH1234ijklMNOP'),
                            ('DATABASE_PASSWORD=hunter2hunter2', 'hunter2hunter2'),
                            ('my password is SuperSecret123', 'SuperSecret123'),
                            ('Authorization: Bearer abcDEF123456ghiJKL789', 'abcDEF123456ghiJKL789'),
                            ('curl -H "Bearer abcDEF123456ghiJKL789" x', 'abcDEF123456ghiJKL789'),
                            ('token=abcdEFGH1234ijklMNOP', 'abcdEFGH1234ijklMNOP')):
            with self.subTest(raw=raw):
                self.assertNotIn(secret, text.redact(raw, 300))
        self.assertEqual(text.redact('export OPENAI_API_KEY=abcdEFGH1234ijklMNOP', 300), 'export OPENAI_API_KEY=<secret>')


class RepairUndo(Isolated):
    def test_uninstall_undoes_an_automatic_toolkit_repair(self):
        settings = os.path.join(self.claude, 'settings.json')
        plugin = install.CLAUDE_CLASH[0]
        store.write_json(settings, {'enabledPlugins': {plugin: True}})
        proposal = review.add(1, 'toolkit_fix', 'clash', 'why', 'fix', [], 'clash-test')
        proposal['repair'] = {'operation': 'disable_plugins', 'plugins': [plugin]}
        autonomy.repair_toolkit(proposal)
        self.assertIs(store.read_json(settings)['enabledPlugins'][plugin], False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(install.uninstall(install.Env(), dry=True, yes=True, purge=False), 0)
            self.assertEqual(install.uninstall(install.Env(), dry=False, yes=True, purge=False), 0)
        self.assertIn('RSI repair', out.getvalue())
        self.assertIs(store.read_json(settings)['enabledPlugins'][plugin], True)

    def test_uninstall_tolerates_legacy_records_without_summary(self):
        target = os.path.join(self.tmp.name, 'f.json')
        store.write_json(target, {'a': 2})
        store.write_json(install.Env().manifest, {'actions': [
            {'kind': 'edit_json', 'file': target, 'undo': {}}]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(install.uninstall(install.Env(), dry=True, yes=True, purge=False), 0)
        self.assertIn('edit_json', out.getvalue())


class InstructionAutonomy(Isolated):
    def proposal(self, action, line, doc):
        with open(os.path.join(self.claude, 'CLAUDE.md'), 'w') as f:
            f.write(doc)
        b = curate.new_bullet('always', 'decide routine choices yourself', 'global', 'preference', 'user-stated', 2,
                              [{'sid': 'x'}], 'active')
        curate.save([b])
        p = review.add(1, 'instr_' + action, 'audit', 'reason', 'fix', [], 'audit:' + line)
        p.update(edit={'file': 'claude', 'action': action, 'line': line, 'replacement': '- replaced'},
                 bullet_ids=[b['id']])
        store.write_json(paths.path('proposals', p['id'] + '.json'), p)
        autonomy.configure(True)
        return p

    def test_autonomy_never_removes_a_user_authored_line(self):
        p = self.proposal('remove', '- Never push to main', '# mine\n- Never push to main\n')
        out = autonomy.run()
        self.assertIn(p['id'], [d['id'] for d in out['deferred']])
        self.assertIn('- Never push to main', open(os.path.join(self.claude, 'CLAUDE.md')).read())
        self.assertEqual(store.read_json(paths.path('proposals', p['id'] + '.json'))['status'], 'pending')

    def test_autonomy_may_edit_lines_inside_the_managed_block(self):
        doc = f'# mine\n- keep\n{instructions.BEGIN}\n- learned old\n{instructions.END}\n'
        p = self.proposal('edit', '- learned old', doc)
        out = autonomy.run()
        self.assertIn(p['id'], out['applied'])
        self.assertIn('- replaced', open(os.path.join(self.claude, 'CLAUDE.md')).read())


if __name__ == '__main__':
    unittest.main()
