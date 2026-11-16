"""Installation is not proof that a particular session receives hook events."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from helpers import REPO
from honey import __version__, doctor, health, paths


class Health(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, HONEY_HOME=str(self.root / 'state'),
                         CLAUDE_CONFIG_DIR=str(self.root / 'claude'), CODEX_HOME=str(self.root / 'codex'))
        env.start()
        self.addCleanup(env.stop)
        self.ctx = SimpleNamespace(agent='codex', sid='session-1', state={}, dirty=False,
                                   payload={'hook_event_name': 'SessionStart'})

    def install(self, agent='codex', enabled=True):
        config = Path(paths.codex_dir() if agent == 'codex' else paths.claude_dir())
        bundle = config / 'plugins/cache/honey-stack/honey/0.11.0'
        (bundle / '.codex-plugin').mkdir(parents=True)
        (bundle / '.codex-plugin/plugin.json').write_text('{"name":"honey"}', encoding="utf-8")
        (bundle / 'hooks').mkdir()
        (bundle / 'hooks/codex-hooks.json').write_text('{}', encoding="utf-8")
        (bundle / 'hooks/hooks.json').write_text('{}', encoding="utf-8")
        (bundle / 'bin').mkdir()
        (bundle / 'bin/honey').write_text('# entrypoint', encoding="utf-8")
        if agent == 'codex':
            (config / 'config.toml').write_text('[plugins."honey@honey-stack"]\nenabled = ' + str(enabled).lower() + '\n', encoding="utf-8")
        else:
            (config / 'settings.json').write_text(json.dumps({'enabledPlugins': {'honey@honey-stack': enabled}}), encoding="utf-8")

    def deliver(self, event, now=1000, **payload):
        self.ctx.payload = dict(payload, hook_event_name=event)
        with patch.object(health.time, 'time', return_value=now):
            health.record(self.ctx)
        state = Path(paths.path('state', 'sessions', 'codex-session-1.json'))
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(self.ctx.state), encoding="utf-8")

    def test_installed_enabled_missing_session_is_not_healthy(self):
        self.install()
        result = health.report('codex', 'session-1', now=1000)
        self.assertTrue(result['installed'])
        self.assertTrue(result['enabled'])
        self.assertFalse(result['receiving'])
        self.assertEqual(result['delivery'], 'missing')
        self.assertEqual(result['coverage']['status'], 'unobserved')
        self.assertFalse(result['healthy'])
        self.assertFalse(Path(paths.home()).exists(), 'read-only report created state')

    def test_delivery_and_supported_coverage_are_separate(self):
        self.install()
        self.deliver('SessionStart')
        first = health.report('codex', 'session-1', now=1001)
        self.assertTrue(first['receiving'])
        self.assertEqual(first['coverage']['status'], 'unobserved')
        self.deliver('PreToolUse', tool_name='functions.exec_command', tool_input={'cmd': 'secret-value'})
        result = health.report('codex', 'session-1', now=1001)
        self.assertEqual(result['coverage']['status'], 'supported')
        self.assertEqual(result['runtime_version'], __version__)
        self.assertTrue(self.ctx.dirty)
        self.assertNotIn('secret-value', json.dumps(self.ctx.state))

    def test_opaque_tools_remain_explicitly_unverified(self):
        self.install()
        self.deliver('PreToolUse', tool_name='functions.exec', tool_input={'code': 'await arbitrary()'})
        result = health.report('codex', 'session-1', now=1001)
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertEqual(result['coverage']['unsupported_tools'], ['exec'])
        self.assertFalse(result['healthy'])
        self.assertEqual(result['enforcement'], 'unverified')

    def test_stale_ended_and_paused_sessions_are_not_active(self):
        self.install()
        self.deliver('SessionStart')
        self.assertEqual(health.report('codex', 'session-1', now=2000)['delivery'], 'stale')
        self.deliver('SessionEnd')
        self.assertEqual(health.report('codex', 'session-1', now=1001)['delivery'], 'ended')
        self.ctx.state['off'] = True
        self.deliver('UserPromptSubmit')
        result = health.report('codex', 'session-1', now=1001)
        self.assertTrue(result['receiving'])
        self.assertTrue(result['paused'])
        self.assertFalse(result['healthy'])

    def test_coverage_metadata_is_bounded(self):
        for i in range(100):
            self.deliver('PreToolUse', tool_name='unknown-' + str(i), tool_input={'secret': 'not logged'})
        for i in range(100):
            self.deliver('arbitrary-event-' + str(i))
        self.assertLessEqual(len(self.ctx.state['health']['unsupported_tools']), 12)
        self.assertLessEqual(len(self.ctx.state['health']['events']), 16)
        self.assertNotIn('not logged', json.dumps(self.ctx.state))

    def test_disabled_config_and_other_agent_do_not_claim_enabled(self):
        self.install(enabled=False)
        self.install('claude', enabled=True)
        self.assertFalse(health.report('codex', 'session-1')['enabled'])
        self.assertTrue(health.report('claude', 'session-1')['enabled'])
        self.assertEqual(health.report('claude', 'session-1')['delivery'], 'missing')

    def test_valid_commented_config_header_is_recognized(self):
        self.install()
        Path(paths.codex_dir(), 'config.toml').write_text(
            "[plugins.'honey@honey-stack'] # installed plugin\nenabled = true # intentional\n", encoding="utf-8")
        self.assertTrue(health.report('codex', 'session-1')['enabled'])

    def test_malformed_state_and_invalid_session_id_are_not_trusted(self):
        self.install()
        self.deliver('SessionStart')
        state = Path(paths.path('state', 'sessions', 'codex-session-1.json'))
        state.write_text('{"health":{"events":[{}],"last_seen":true,"unsupported_tools":[null]}}', encoding="utf-8")
        self.assertEqual(health.report('codex', 'session-1')['delivery'], 'missing')
        self.assertEqual(health.report('codex', '../session-1')['delivery'], 'invalid_session')
        state.write_text(json.dumps({'health': {'last_seen': 10**400}}), encoding="utf-8")
        self.assertEqual(health.report('codex', 'session-1')['delivery'], 'missing')

    def test_json_doctor_reports_missing_delivery_without_smoke_or_writes(self):
        self.install()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch.object(doctor, 'smoke', side_effect=AssertionError('not live delivery')):
            code = doctor.main(['--agent', 'codex', '--session', 'session-1', '--json'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())['delivery'], 'missing')
        self.assertFalse(Path(paths.home()).exists())


if __name__ == '__main__':
    unittest.main()
