"""Task scope and fresh, observed completion evidence through real hook/CLI entrypoints."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import REPO, hook


class TaskEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, HONEY_HOME=self.tmp.name, HONEY_NO_AUTOUPDATE='1')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.base = {'session_id': 'task', 'cwd': self.tmp.name}

    def event(self, name, **fields):
        return hook('codex', dict(self.base, hook_event_name=name, **fields))

    def state(self):
        return json.loads(Path(self.tmp.name, 'state/sessions/codex-task.json').read_text())

    def cli(self, *args):
        return subprocess.run([sys.executable, str(Path(REPO, 'bin/honey')), 'task', *args,
                               '--agent', 'codex', '--session', 'task'], capture_output=True, text=True)

    def begin_code(self):
        self.event('UserPromptSubmit', prompt='Fix the date parser')
        self.event('PostToolUse', tool_name='apply_patch', tool_input={'patch': '*** Begin Patch\n*** Update File: app.py\n@@\n-old\n+new\n*** End Patch'},
                   tool_response={'exit_code': 0}, tool_use_id='edit-1')

    def gap(self, message):
        """Stop is never blocked; return the advisory gap honey recorded for it, or None."""
        self.assertEqual(self.event('Stop', last_assistant_message=message), {})
        rows = [json.loads(line) for path in Path(self.tmp.name, 'events').glob('*/codex-task.jsonl')
                for line in path.read_text().splitlines()]
        stops = [i for i, r in enumerate(rows) if r['e'] == 'stop']
        after = rows[stops[-1] + 1:] if stops else []
        return next((r['reason'] for r in after if r['e'] == 'receipt_gap'), None)

    def check(self, code=0, call='test-1', cwd=None):
        return self.event('PostToolUse', tool_name='exec_command', tool_input={'cmd': 'python -m unittest', 'workdir': cwd or self.tmp.name},
                          tool_response={} if code is None else {'exit_code': code}, tool_use_id=call)

    def test_read_only_is_retained_on_continue_then_explicit_action_changes_task(self):
        self.event('UserPromptSubmit', prompt='Only explain the parser. Do not change files.')
        original = self.state()['task']
        self.assertEqual(original['mode'], 'inspect')
        self.event('UserPromptSubmit', prompt='continue')
        self.assertEqual(self.state()['task']['id'], original['id'])
        self.assertEqual(self.state()['task']['mode'], 'inspect')
        self.event('UserPromptSubmit', prompt='Now implement the parser fix and push it')
        task = self.state()['task']
        self.assertEqual(task['mode'], 'execute')
        self.assertNotEqual(task['id'], original['id'])
        self.assertIn('push', task['approved_actions'])
        self.event('UserPromptSubmit', prompt='continue')
        self.assertEqual(self.state()['task']['approved_actions'], task['approved_actions'])
        self.event('UserPromptSubmit', prompt="also don't push")
        self.assertNotIn('push', self.state()['task']['approved_actions'])

    def test_direct_action_confirmation_exits_inspection_without_magic_phrase(self):
        for prompt in ('do it', 'go ahead', 'please do it', 'yes, do it'):
            self.event('UserPromptSubmit', prompt='Only inspect the parser')
            self.event('UserPromptSubmit', prompt=prompt)
            self.assertEqual(self.state()['task']['mode'], 'execute', prompt)
        self.event('UserPromptSubmit', prompt='Only inspect the parser')
        for prompt in ('continue', 'yes', 'yup', 'go ahead but do not change files'):
            self.event('UserPromptSubmit', prompt=prompt)
            self.assertEqual(self.state()['task']['mode'], 'inspect', prompt)

    def test_ordinary_requests_and_casual_confirmations_can_edit(self):
        actions = ('do it than', 'do it then', 'go ahead and make these changes', 'ok go ahead bro',
                   'make the buttons white', 'use white text on the buttons', 'why is the button text dark? make it white',
                   'fix this bro and merge in the main and also install after it shipped')
        for prompt in actions:
            self.event('UserPromptSubmit', prompt='Only inspect the parser')
            self.event('UserPromptSubmit', prompt=prompt)
            self.assertEqual(self.state()['task']['mode'], 'execute', prompt)
        # Inspection that came from a question never traps a plain request.
        for prompt in actions + ('i want white text on the buttons', 'the cta text should be white', 'continue'):
            self.event('UserPromptSubmit', prompt='why is the button text dark?')
            self.event('UserPromptSubmit', prompt=prompt)
            self.assertEqual(self.state()['task']['mode'], 'execute', prompt)
        self.event('UserPromptSubmit', prompt='Only inspect the parser')
        self.event('UserPromptSubmit', prompt='i want it simpler')
        self.assertEqual(self.state()['task']['mode'], 'inspect')
        for prompt in ("don't fix it yet, just look", 'never touch the parser'):
            self.event('UserPromptSubmit', prompt=prompt)
            self.assertEqual(self.state()['task']['mode'], 'inspect', prompt)

    def test_questions_stay_read_only_until_any_confirmation(self):
        for prompt in ('getting this error, why is that?', 'What should we change in this file?',
                       'how does the parser work', 'Make recommendations only', 'no code changes, just review it'):
            self.event('UserPromptSubmit', prompt=prompt)
            self.assertEqual(self.state()['task']['mode'], 'inspect', prompt)
        self.event('UserPromptSubmit', prompt='Can you make the buttons white?')
        self.assertEqual(self.state()['task']['mode'], 'inspect')
        self.event('UserPromptSubmit', prompt='yes')
        self.assertEqual(self.state()['task']['mode'], 'execute')

    def test_stop_gap_recorded_once_per_edit_turn_and_never_blocks(self):
        self.begin_code()
        self.assertIn('verification', self.gap('Fixed.'))
        self.assertIsNone(self.gap('Fixed.'))
        self.event('PostToolUse', tool_name='Edit', tool_input={'file_path': '/tmp/a', 'new_string': 'again'})
        self.assertIsNone(self.gap('Fixed.'))
        self.event('UserPromptSubmit', prompt='continue')
        self.assertIsNone(self.gap('Fixed.'))  # no edit this turn: a plain answer is never judged
        self.event('PostToolUse', tool_name='Edit', tool_input={'file_path': '/tmp/a', 'new_string': 'more'})
        self.assertIsNotNone(self.gap('Fixed.'))

    def test_failed_read_command_does_not_invalidate_successful_check(self):
        self.begin_code()
        self.check()
        for i in range(3):
            self.event('PostToolUseFailure', tool_name='exec_command', tool_input={'cmd': 'pwd'},
                       error='shell initialization failed: zoxide', tool_use_id='read-' + str(i))
        self.assertIsNone(self.gap('Fixed. Checks run: unittest pass.'))

    def test_receipt_words_without_observed_check_do_not_verify_code(self):
        self.begin_code()
        result = self.gap('Fixed. Checks run: all passed.')
        self.assertIsNotNone(result)
        self.assertIn('evidence', result)

    def test_successful_check_after_latest_edit_allows_completion(self):
        self.begin_code()
        self.check()
        self.assertIsNone(self.gap('Fixed. Checks run: unittest pass.'))
        self.event('PostToolUse', tool_name='Edit', tool_input={'file_path': str(Path(self.tmp.name, 'app.py')), 'new_string': 'again'}, tool_use_id='edit-2')
        self.assertIsNotNone(self.gap('Fixed. Checks run: unittest pass.'))

    def test_failed_unknown_and_unrelated_checks_never_count_as_success(self):
        self.begin_code()
        self.check(cwd='/elsewhere')
        self.assertFalse(self.state()['task']['evidence'])
        self.check(code=None)
        self.assertFalse(self.state()['task']['evidence'])
        self.check(call='pass')
        self.check(code=1, call='fail')
        self.assertIsNotNone(self.gap('Fixed. Checks run: tests pass.'))

    def test_ui_removal_keeps_normal_verification(self):
        for prompt in ('Remove the app logo from the header', 'Remove this button completely',
                       'Remove the application sidebar', 'Remove the software version label',
                       'Remove the uninstall button', 'Remove the app icon from the dock',
                       'Remove a button from the app', 'Remove the login page completely from the app',
                       'Remove the uninstall menu item', 'Remove the uninstall confirmation dialog'):
            with self.subTest(prompt=prompt):
                self.event('UserPromptSubmit', prompt=prompt)
                task = self.state()['task']
                self.assertEqual(task['mode'], 'execute')
                self.assertEqual(task['operation'], 'generic')
                self.assertEqual(task['checks'], ['verification'])

    def test_actual_app_removal_keeps_uninstall_checks(self):
        for prompt in ('Uninstall VLC', 'Remove Example.app completely',
                       'Remove the VLC app from my Mac', 'Remove this application',
                       'Remove /Applications/Example.app', 'Remove VLC.app and its helper files',
                       'Remove the application VLC', 'Remove the app VLC', 'Remove all apps except VLC'):
            with self.subTest(prompt=prompt):
                self.event('UserPromptSubmit', prompt=prompt)
                task = self.state()['task']
                self.assertEqual(task['operation'], 'uninstall')
                self.assertEqual(task['checks'], ['application', 'helpers', 'startup', 'preserved-data'])

    def test_uninstall_requires_all_declared_checks_and_absence_is_measured(self):
        self.event('UserPromptSubmit', prompt='Remove Example.app completely')
        app = Path(self.tmp.name, 'Example.app')
        app.mkdir()
        result = self.cli('set', '--mode', 'execute', '--operation', 'uninstall', '--target', str(app))
        self.assertEqual(result.returncode, 0, result.stderr)
        failed = self.cli('verify', '--check', 'application', '--absent', str(app))
        self.assertNotEqual(failed.returncode, 0)
        app.rmdir()
        self.assertEqual(self.cli('verify', '--check', 'application', '--absent', str(app)).returncode, 0)
        app.mkdir()
        status = json.loads(self.cli('status', '--json').stdout)
        self.assertIn('application', status['missing_checks'])
        app.rmdir()
        result = self.gap('Completely removed. Checks run: application absent.')
        self.assertIsNotNone(result)
        self.assertIn('startup', result)
        self.assertIn('helpers', result)

    def test_uninstall_application_cannot_be_proven_by_presence(self):
        self.event('UserPromptSubmit', prompt='Remove Example.app completely')
        app = Path(self.tmp.name, 'Example.app')
        app.mkdir()
        self.assertEqual(self.cli('set', '--mode', 'execute', '--operation', 'uninstall', '--target', str(app)).returncode, 0)
        self.assertNotEqual(self.cli('verify', '--check', 'application', '--present', str(app)).returncode, 0)

    def test_new_task_cannot_reuse_proof_or_approval(self):
        self.begin_code()
        self.check()
        self.event('UserPromptSubmit', prompt='Only inspect the other project')
        task = self.state()['task']
        self.assertEqual(task['mode'], 'inspect')
        self.assertFalse(task['evidence'])
        self.assertFalse(task['approved_actions'])

    def test_inferred_inspection_does_not_block_edits_or_evidence_contract(self):
        self.event('UserPromptSubmit', prompt='What should we change in this file?')
        self.assertEqual(self.state()['task']['mode'], 'inspect')
        self.assertEqual(self.event('PreToolUse', tool_name='Edit', tool_input={'file_path': '/tmp/synthetic', 'new_string': 'x'}), {})
        self.assertEqual(self.event('PreToolUse', tool_name='exec_command', tool_input={'cmd': 'touch synthetic.txt'}), {})
        self.assertEqual(self.event('PreToolUse', tool_name='exec_command', tool_input={'cmd': 'cat app.py'}), {})
        result = self.cli('set', '--mode', 'execute')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_agent_task_set_keeps_explicit_read_only(self):
        self.event('UserPromptSubmit', prompt='Only inspect the parser')
        self.assertEqual(self.cli('set', '--mode', 'inspect').returncode, 0)
        self.event('UserPromptSubmit', prompt='yes')
        self.assertEqual(self.state()['task']['mode'], 'inspect')

    def test_tool_mapping_rejects_failed_stale_and_unrelated_call_ids(self):
        self.begin_code()
        self.check(code=1, call='bad')
        self.assertNotEqual(self.cli('verify', '--check', 'verification', '--tool-call', 'bad').returncode, 0)
        self.check(call='good')
        self.assertEqual(self.cli('verify', '--check', 'verification', '--tool-call', 'good').returncode, 0)
        self.event('PostToolUse', tool_name='Edit', tool_input={'file_path': '/tmp/a', 'new_string': 'new'}, tool_use_id='edit-new')
        self.assertNotEqual(self.cli('verify', '--check', 'verification', '--tool-call', 'good').returncode, 0)

    def test_running_rerun_invalidates_earlier_pass(self):
        self.begin_code()
        self.check(call='pass')
        self.check(code=None, call='running')
        self.assertFalse(self.state()['task']['evidence'])

    def test_different_passing_check_cannot_hide_a_failed_test(self):
        self.begin_code()
        self.check(code=1)
        self.event('PostToolUse', tool_name='exec_command', tool_input={'cmd': 'git diff --check'},
                   tool_response={'exit_code': 0}, tool_use_id='lint')
        result = self.gap('Fixed. Checks run: all passed.')
        self.assertIsNotNone(result)
        self.assertIn('failed-or-incomplete', result)

    def test_check_started_before_edit_cannot_prove_new_state(self):
        self.begin_code()
        self.event('PreToolUse', tool_name='exec_command', tool_input={'cmd': 'python -m unittest'}, tool_use_id='early')
        self.event('PostToolUse', tool_name='Edit', tool_input={'file_path': '/tmp/a', 'new_string': 'x'}, tool_use_id='later-edit')
        self.check(call='early')
        self.assertFalse(self.state()['task']['evidence'])
        self.check(call='fresh')
        self.assertTrue(self.state()['task']['evidence'])

    def test_completion_from_previous_task_does_not_verify_new_task(self):
        self.begin_code()
        self.event('PreToolUse', tool_name='exec_command', tool_input={'cmd': 'python -m unittest'}, tool_use_id='old')
        self.event('UserPromptSubmit', prompt='Implement the other parser')
        self.check(call='old')
        self.assertFalse(self.state()['task']['evidence'])

    def test_failure_event_invalidates_earlier_success(self):
        self.begin_code()
        self.check(call='pass')
        self.event('PostToolUseFailure', tool_name='exec_command', tool_input={'cmd': 'python -m unittest'}, error='cancelled', tool_use_id='failed')
        self.assertFalse(self.state()['task']['evidence'])

    def test_changed_directory_and_multiline_commands_do_not_auto_verify(self):
        self.begin_code()
        for i, cmd in enumerate(['git -C /elsewhere diff --check', 'pytest\necho success', 'pytest --rootdir=/elsewhere', 'pytest /elsewhere']):
            self.event('PostToolUse', tool_name='exec_command', tool_input={'cmd': cmd}, tool_response={'exit_code': 0}, tool_use_id='c' + str(i))
            self.assertFalse(self.state()['task']['evidence'], cmd)

    def test_explicit_user_skill_mention_belongs_to_new_task(self):
        self.event('UserPromptSubmit', prompt='Implement the parser using $tdd')
        state = self.state()
        self.assertTrue(state['pending_skills'])
        self.assertTrue(all(p['task'] == state['task']['id'] for p in state['pending_skills']))

    def test_common_execution_requests_and_explicit_scope_upgrade(self):
        for prompt in ('Create a parser', 'Add a unit test', 'Refactor the parser', 'Write tests for the parser',
                       'Implement the fix but do not change the API'):
            self.event('UserPromptSubmit', prompt=prompt)
            self.assertEqual(self.state()['task']['mode'], 'execute', prompt)
        self.event('UserPromptSubmit', prompt='Only inspect the parser')
        self.event('UserPromptSubmit', prompt='also implement it')
        self.assertEqual(self.state()['task']['mode'], 'execute')

    def test_status_interrupt_retains_execution_task(self):
        self.event('UserPromptSubmit', prompt='Fix the parser and push')
        original = self.state()['task']
        self.event('UserPromptSubmit', prompt='What is the status?')
        self.event('UserPromptSubmit', prompt='continue')
        self.assertEqual(self.state()['task']['id'], original['id'])
        self.assertIn('push', self.state()['task']['approved_actions'])

    def test_unknown_mutating_shell_invalidates_previous_proof(self):
        self.begin_code()
        self.check()
        self.event('PostToolUse', tool_name='exec_command', tool_input={'cmd': "sed -i '' 's/old/new/' app.py"},
                   tool_response={'exit_code': 0}, tool_use_id='sed')
        self.assertFalse(self.state()['task']['evidence'])

    def test_failed_manual_check_invalidates_and_cannot_remap_older_success(self):
        self.event('UserPromptSubmit', prompt='Disable startup login items')
        for code, call in [(0, 'good'), (1, 'bad')]:
            self.event('PostToolUse', tool_name='exec_command', tool_input={'cmd': 'test -f marker'},
                       tool_response={'exit_code': code}, tool_use_id=call)
            if code == 0:
                self.assertEqual(self.cli('verify', '--check', 'startup', '--tool-call', 'good').returncode, 0)
        self.assertIn('startup', json.loads(self.cli('status').stdout)['missing_checks'])
        self.assertNotEqual(self.cli('verify', '--check', 'startup', '--tool-call', 'good').returncode, 0)

    def test_explicit_workspace_binding_accepts_only_new_workspace(self):
        self.begin_code()
        project = Path(self.tmp.name, 'project')
        project.mkdir()
        self.check(call='wrong', cwd=str(project))
        self.assertFalse(self.state()['task']['evidence'])
        result = self.cli('set', '--mode', 'execute', '--operation', 'code', '--cwd', str(project))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.check(call='right', cwd=str(project))
        self.assertTrue(self.state()['task']['evidence'])

    def test_honest_unverified_report_is_not_a_completion_claim(self):
        self.begin_code()
        self.assertIsNone(self.gap('Not fixed yet; verification remains unverified. Checks run: failed.'))

    def test_negated_unverified_claim_still_requires_evidence(self):
        self.begin_code()
        result = self.gap('Fixed. No unverified checks remain. Checks run: all passed.')
        self.assertIsNotNone(result)

    def test_task_bookkeeping_hook_does_not_erase_the_evidence_it_just_saved(self):
        self.begin_code()
        self.check()
        self.assertEqual(self.cli('verify', '--check', 'verification', '--tool-call', 'test-1').returncode, 0)
        self.event('PostToolUse', tool_name='exec_command', tool_input={'cmd': 'honey task verify --check verification --tool-call test-1'},
                   tool_response={'exit_code': 0}, tool_use_id='bookkeeping')
        self.assertFalse(json.loads(self.cli('status').stdout)['missing_checks'])


if __name__ == '__main__':
    unittest.main()
