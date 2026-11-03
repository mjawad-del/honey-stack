"""Project opt-in and automatic observer boundaries; no real uploads or services."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import helpers
from honey import delta_service
from honey import delta


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'plain-workspace'
        self.project.mkdir()
        self.env = patch.dict(os.environ, {'HONEY_HOME': str(self.root / 'honey'),
                                           'CODEX_HOME': str(self.root / 'codex')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_explicit_opt_in_supports_non_git_workspace_and_off(self):
        self.assertFalse(delta_service.project_status(self.project)['enabled'])
        first = delta_service.configure(self.project, 'org_example', True)
        self.assertTrue(first['enabled'])
        self.assertEqual(first['project'], str(self.project))
        self.assertEqual(delta_service.configure(self.project, 'org_example', True)['since'], first['since'])
        self.assertFalse(delta_service.configure(self.project, None, False)['enabled'])
        config = self.root / 'honey' / 'delta' / 'projects.json'
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.project / '.git').exists())

    def test_existing_status_exposes_codex_activation(self):
        delta_service.configure(self.project, 'org_example', True)
        result = delta.capture_status(self.project)
        self.assertTrue(result['configured'])
        self.assertTrue(result['codex_capture'])
        self.assertEqual(result['mode'], 'private')
        self.assertEqual(result['codex']['content'], 'visible user and assistant text')

    def test_disabled_observer_never_opens_transcripts(self):
        with patch.object(delta_service, '_session_files', side_effect=AssertionError('must not read')):
            result = delta_service.run_once()
        self.assertEqual(result['sessions'], [])

    def test_changed_file_runs_once_and_failed_delivery_backs_off(self):
        policy = delta_service.configure(self.project, 'org_example', True)
        file = self.root / 'codex/sessions/2026/11/04/rollout-test.jsonl'
        file.parent.mkdir(parents=True)
        file.write_text('{}\n')
        success = {'status': 'synced', 'pending': 0, 'thread': 'thread_test', 'cloud': {'state': 'uploaded'}}
        with patch('honey.delta_capture.capture_file', return_value=success) as capture:
            delta_service.run_once()
            delta_service.run_once()
            self.assertEqual(capture.call_count, 1)
            self.assertEqual(capture.call_args.args[1:4], (str(self.project), policy['since'], 'org_example'))
            file.write_text('{}\n{}\n')
            capture.return_value = {'status': 'error', 'error': 'timeout', 'pending': 1, 'retryable': True}
            delta_service.run_once()
            file.write_text('{}\n{}\n{}\n')
            delta_service.run_once()
            self.assertEqual(capture.call_count, 2)
        self.assertEqual(delta_service.status(self.project)['sessions'][0]['error'], 'timeout')

    def test_pending_delivery_does_not_disappear_when_transcript_is_removed(self):
        delta_service.configure(self.project, 'org_example', True)
        file = self.root / 'codex/sessions/rollout-test.jsonl'
        file.parent.mkdir(parents=True)
        file.write_text('{}\n')
        with patch('honey.delta_capture.capture_file', return_value={
                'status': 'error', 'error': 'timeout', 'pending': 1, 'retryable': True}):
            delta_service.run_once()
            file.unlink()
            result = delta_service.run_once()
        self.assertEqual(len(result['sessions']), 1)
        self.assertEqual(result['sessions'][0]['error'], 'transcript_unavailable')
        self.assertEqual(result['sessions'][0]['pending'], 1)

    def test_nonretryable_failure_waits_for_explicit_retry(self):
        delta_service.configure(self.project, 'org_example', True)
        file = self.root / 'codex/sessions/rollout-test.jsonl'
        file.parent.mkdir(parents=True)
        file.write_text('{}\n')
        with patch('honey.delta_capture.capture_file', return_value={
                'status': 'error', 'error': 'producer_sequence_mismatch', 'retryable': False}) as capture:
            delta_service.run_once()
            delta_service.run_once()
            self.assertEqual(capture.call_count, 1)
            delta_service.run_once(retry_errors=True)
            self.assertEqual(capture.call_count, 2)

    def test_disable_stops_pending_upload_and_reenable_starts_new_epoch(self):
        with patch.object(delta_service, 'utc_now', return_value='2026-11-04T01:00:00Z'):
            first = delta_service.configure(self.project, 'org_example', True)
        old_dir = delta_service._state_directory(str(self.project), first)
        delta_service.configure(self.project, None, False)
        with patch.object(delta_service, '_session_files', side_effect=AssertionError('disabled')):
            delta_service.run_once()
        with patch.object(delta_service, 'utc_now', return_value='2026-11-04T02:00:00Z'):
            second = delta_service.configure(self.project, 'org_example', True)
        self.assertNotEqual(old_dir, delta_service._state_directory(str(self.project), second))

    def test_corrupt_registry_is_not_replaced(self):
        delta_service.configure(self.project, 'org_example', True)
        file = self.root / 'honey/delta/projects.json'
        file.write_text('{broken')
        with self.assertRaises(delta.DeltaError):
            delta_service.configure(self.project, 'org_example', True)
        self.assertEqual(file.read_text(), '{broken')

    def test_invalid_nested_state_reports_named_error(self):
        delta_service.configure(self.project, 'org_example', True)
        file = self.root / 'honey/delta/projects.json'
        data = json.loads(file.read_text())
        data['projects'][str(self.project)]['since'] = 123
        file.write_text(json.dumps(data))
        with self.assertRaises(delta.DeltaError):
            delta_service.status()
        data['projects'][str(self.project)]['since'] = '2026-11-04T00:00:00Z'
        file.write_text(json.dumps(data))
        observer = file.parent / 'observer.json'
        for malformed in ({'sessions': []}, {'sessions': {'x': 1}}, {'checked_at': 123}):
            observer.write_text(json.dumps(malformed))
            with self.assertRaises(delta.DeltaError):
                delta_service.status()

    def test_removed_workspace_can_be_inspected_and_disabled(self):
        delta_service.configure(self.project, 'org_example', True)
        self.project.rmdir()
        self.assertTrue(delta_service.status()['projects'][0]['enabled'])
        self.assertFalse(delta_service.configure(self.project, None, False)['enabled'])

    def test_symlinked_session_files_are_not_observed(self):
        sessions = self.root / 'codex/sessions'
        sessions.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'rollout-hidden.jsonl').write_text('{}\n')
        (sessions / 'linked').symlink_to(outside, target_is_directory=True)
        (sessions / 'rollout-link.jsonl').symlink_to(outside / 'rollout-hidden.jsonl')
        self.assertEqual(delta_service._session_files(sessions, 0), [])

    def test_launch_agent_uses_stable_launcher_and_preserves_homes(self):
        import plistlib
        launcher = self.root / '.local/bin/honey'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('# launcher')
        completed = type('Completed', (), {'returncode': 0})()
        with patch.object(delta_service.Path, 'home', return_value=self.root), \
                patch.object(delta_service.sys, 'platform', 'darwin'), \
                patch.object(delta_service, 'service_status', return_value={'registered': False}), \
                patch.object(delta_service.subprocess, 'run', return_value=completed) as command:
            delta_service.install_service()
        definition = plistlib.loads((self.root / 'Library/LaunchAgents/dev.honey.delta-capture.plist').read_bytes())
        self.assertEqual(definition['ProgramArguments'][1:], [str(launcher), 'delta', 'capture', 'run', '--once'])
        self.assertEqual(definition['StartInterval'], 15)
        self.assertEqual(definition['EnvironmentVariables']['HONEY_HOME'], str(self.root / 'honey'))
        self.assertEqual(command.call_args.args[0][1], 'bootstrap')

    def test_cli_status_does_not_enable_capture(self):
        import subprocess
        import sys
        command = subprocess.run([sys.executable, str(Path(helpers.REPO) / 'bin/honey'),
                                  'delta', 'capture', 'status', '--project', str(self.project)],
                                 capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertFalse(json.loads(command.stdout)['projects'][0]['enabled'])


if __name__ == '__main__':
    unittest.main()
