"""Response modes through the public hook and CLI entrypoints."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import REPO, ctx_text, hook
from honey import modes


class ResponseModes(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        # A detached RSI job may still hold logs/jobs.log open (Windows refuses to delete it then).
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.env = patch.dict(os.environ, HONEY_HOME=str(self.home), HONEY_NO_AUTOUPDATE='1')
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_switches_response_mode_and_keeps_it_after_compaction(self):
        for agent in ('codex', 'claude'):
            payload = {'session_id': 'modes', 'cwd': REPO}
            boot = ctx_text(hook(agent, dict(payload, hook_event_name='SessionStart', source='startup')))
            self.assertTrue(boot.startswith('honey-stack active · mode=direct.'), boot)
            for mode, guidance in (('direct', 'concise'), ('explain', 'comprehensive'), ('balance', 'moderate detail')):
                result = ctx_text(hook(agent, dict(payload, hook_event_name='UserPromptSubmit', prompt='honey ' + mode)))
                self.assertIn('HONEY MODE ' + mode, result)
                self.assertIn('`🍯 HONEY · ' + mode + '`', result)
                self.assertIn(guidance, result)
                resumed = ctx_text(hook(agent, dict(payload, hook_event_name='SessionStart', source='compact')))
                self.assertIn('honey-stack active · mode=' + mode + '.', resumed)
                self.assertIn(guidance, resumed)
                self.assertNotIn('scope=', resumed)
                self.assertNotIn('process=', resumed)

    def cli(self, *args, payload=None):
        result = subprocess.run([sys.executable, str(Path(REPO, 'bin/honey')), *args],
                                input=json.dumps(payload) if payload else '', capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_saved_defaults_preserve_other_settings_and_apply_to_new_sessions(self):
        config = self.home / 'config.json'
        config.write_text(json.dumps({'modes': {'scope': 'ultra', 'process': 'strict', 'voice': 'terse'},
                                      'budgets': {'enabled': False}, 'rsi': {'autonomous': True}}), encoding="utf-8")
        for mode in ('explain', 'balance', 'direct'):
            with self.subTest(mode=mode):
                result = ctx_text(hook('codex', {'session_id': 'saving', 'cwd': REPO,
                                                'hook_event_name': 'UserPromptSubmit',
                                                'prompt': '$honey default ' + mode}))
                self.assertIn('(saved as default)', result)
                self.assertEqual(json.loads(config.read_text(encoding="utf-8")),
                                 {'mode': mode, 'budgets': {'enabled': False}, 'rsi': {'autonomous': True}})
                boot = ctx_text(hook('codex', {'session_id': 'new-' + mode, 'cwd': REPO,
                                              'hook_event_name': 'SessionStart', 'source': 'startup'}))
                self.assertIn('honey-stack active · mode=' + mode + '.', boot)

    def test_legacy_settings_are_read_without_writing_during_bootstrap(self):
        config = self.home / 'config.json'
        for voice, expected in (('terse', 'direct'), ('normal', 'balance')):
            with self.subTest(voice=voice):
                config.write_text(json.dumps({'modes': {'scope': 'ultra', 'process': 'strict', 'voice': voice}}), encoding="utf-8")
                before = config.read_bytes()
                output = self.cli('bootstrap', '--agent', 'codex', '--session', 'missing')
                self.assertIn('🍯 HONEY · ' + expected + ' · hooks unverified', output)
                self.assertEqual(config.read_bytes(), before)
                self.assertNotIn('scope=', output)
                self.assertNotIn('process=', output)

    def test_bootstrap_routes_update_hooks_even_when_hooks_are_unverified(self):
        output = self.cli('bootstrap', '--agent', 'codex', '--session', 'missing')
        self.assertIn('hooks unverified', output)
        self.assertIn('update hooks', output)
        self.assertIn('skills/honey/references/update-hooks.md', output)

    def test_existing_session_choice_wins_over_the_global_default(self):
        (self.home / 'config.json').write_text('{"mode":"explain"}', encoding="utf-8")
        state = self.home / 'state/sessions/claude-legacy.json'
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'started': 'earlier', 'scope': 'ultra', 'process': 'strict',
                                     'voice': 'normal', 'project': 'sample', 'brief': '', 'turn': 1}), encoding="utf-8")
        self.assertIn('[HONEY:balance]', self.cli('statusline', payload={'session_id': 'legacy'}))
        self.assertIn('mode=balance.', self.cli('bootstrap', '--agent', 'claude', '--session', 'legacy'))
        resumed = ctx_text(hook('claude', {'session_id': 'legacy', 'cwd': REPO,
                                          'hook_event_name': 'SessionStart', 'source': 'compact'}))
        self.assertIn('mode=balance.', resumed)
        saved = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(saved['mode'], 'balance')
        self.assertFalse({'scope', 'process', 'voice'} & set(saved))
        self.assertIn('[HONEY:explain]', self.cli('statusline', payload={'session_id': 'missing'}))

    def test_commands_reject_conflicts_and_preserve_pause_and_default(self):
        for prompt in ('honey direct explain', 'honey on off', 'honey status direct', 'honey off default',
                       'honey default', 'honey ultra strict', 'please explain this', 'add a balance option'):
            with self.subTest(prompt=prompt):
                self.assertIsNone(modes.parse_command(prompt))
        for command in ('honey explain', '$honey balance', '/honey:honey direct', '@honey direct default'):
            self.assertIsNotNone(modes.parse_command(command))
        payload = {'session_id': 'pause', 'cwd': REPO, 'hook_event_name': 'UserPromptSubmit'}
        hook('codex', dict(payload, prompt='honey balance default'))
        hook('codex', dict(payload, prompt='honey off'))
        hook('codex', dict(payload, prompt='honey explain'))
        status = ctx_text(hook('codex', dict(payload, prompt='honey status')))
        self.assertIn('HONEY MODE explain (paused)', status)
        self.assertIn('[HONEY:balance]', self.cli('statusline', payload={'session_id': 'missing'}))
        resumed = ctx_text(hook('codex', dict(payload, prompt='honey on')))
        self.assertIn('HONEY MODE explain (this session)', resumed)

    def test_invalid_saved_values_fall_back_to_direct(self):
        config = self.home / 'config.json'
        for value in (None, [], True, {'mode': 'unknown'}, {'modes': None}, {'mode': []}, {'modes': {'voice': []}}):
            with self.subTest(value=value):
                config.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(modes.defaults(), {'mode': 'direct'})


if __name__ == '__main__':
    unittest.main()
