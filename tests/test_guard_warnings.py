"""Learned advice must not become permission denial at the host boundary."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from helpers import hook
from honey.rsi import guards


class GuardWarnings(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = patch.dict(os.environ, HONEY_HOME=self.home.name, HONEY_NO_AUTOUPDATE='1')
        env.start()
        self.addCleanup(env.stop)

    def configure(self, action=None):
        rule = {'id': 'g-advice', 'status': 'active', 'regex': r'^touch\b',
                'message': 'Consider adding a test for the new fixture.'}
        if action is not None:
            rule['action'] = action
        guards.save([rule])

    def pretool(self, agent, command='touch fixture.txt', mode='default'):
        return hook(agent, {'session_id': 'warning', 'cwd': self.home.name,
                           'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
                           'tool_input': {'command': command}, 'permission_mode': mode})

    def test_warning_never_grants_or_denies_permission(self):
        for action in ('warn', None):
            self.configure(action)
            for agent in ('codex', 'claude'):
                for mode in ('default', 'bypassPermissions', 'dontAsk'):
                    with self.subTest(action=action, agent=agent, mode=mode):
                        out = self.pretool(agent, mode=mode)
                        self.assertNotIn('permissionDecision', out.get('hookSpecificOutput', {}))
                        self.assertNotIn('decision', out)
                        if agent == 'claude':
                            self.assertIn('Consider adding', out['hookSpecificOutput']['additionalContext'])
        events = [json.loads(line) for path in Path(self.home.name, 'events').glob('*/*.jsonl')
                  for line in path.read_text().splitlines()]
        self.assertTrue(any(e.get('e') == 'guard' and e.get('learned') and e.get('decision') == 'warn'
                            for e in events))

    def test_explicit_learned_block_remains_a_block(self):
        self.configure('block')
        for agent in ('codex', 'claude'):
            self.assertEqual(self.pretool(agent)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_warning_cannot_hide_a_later_block(self):
        self.configure('warn')
        warning = guards.load()[0]
        block = dict(warning, id='g-block', action='block', message='A separate blocking rule applies.')
        for rules in ([warning, block], [block, warning]):
            guards.save(rules)
            for agent in ('codex', 'claude'):
                out = self.pretool(agent)['hookSpecificOutput']
                self.assertEqual(out['permissionDecision'], 'deny')
                self.assertEqual(out['permissionDecisionReason'], block['message'])

    def test_exempt_block_does_not_hide_a_warning(self):
        self.configure('warn')
        warning = guards.load()[0]
        block = dict(warning, id='g-block', action='block', unless_prompt='approved')
        for rules in ([warning, block], [block, warning]):
            guards.save(rules)
            for agent in ('codex', 'claude'):
                hook(agent, {'session_id': 'warning', 'cwd': self.home.name,
                             'hook_event_name': 'UserPromptSubmit', 'prompt': 'approved'})
                self.assertNotIn('permissionDecision', self.pretool(agent).get('hookSpecificOutput', {}))

    def test_destructive_command_guard_remains_effective(self):
        self.configure('warn')
        for agent in ('codex', 'claude'):
            out = self.pretool(agent, command='rm -rf /')
            self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')
            self.assertIn('recursive force deletion', out['hookSpecificOutput']['permissionDecisionReason'])


if __name__ == '__main__':
    unittest.main()
