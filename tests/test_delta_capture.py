"""Exercise transcript capture through the real Delta executable boundary."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from helpers import REPO
from honey import delta_capture


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.states = self.root / 'capture'
        self.session = '00000000-1111-4222-8333-444444444444'
        self.transcript = self.root / ('rollout-2026-11-04T00-00-00-' + self.session + '.jsonl')
        self.since = '2026-11-04T00:00:00Z'
        self.remote = self.root / 'remote.json'
        self.calls = self.root / 'calls.jsonl'
        self.env = patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ.get('PATH', ''),
                              CAPTURE_REMOTE=str(self.remote), CAPTURE_CALLS=str(self.calls))
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.root / 'delta').write_text('#!' + sys.executable + '\n' + FAKE_DELTA)
        (self.root / 'delta').chmod(0o700)
        self.write([self.meta(), self.message('u1', 'user', 'Remember the final choice.'),
                    self.message('a1', 'assistant', 'The final choice is blue.')])

    def meta(self, **overrides):
        return {'timestamp': self.since, 'type': 'session_meta', 'payload': {
            'id': self.session, 'session_id': self.session, 'cwd': str(self.project),
            'source': 'vscode', **overrides}}

    def message(self, ident, role, text, stamp='2026-11-04T00:01:00Z', **overrides):
        payload = {'type': 'message', 'id': ident, 'role': role,
                   'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}],
                   'internal_chat_message_metadata_passthrough': {
                       'create_time': datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp(),
                       'content_item_kinds': ['user.text'] if role == 'user' else ['unknown'],
                       'content_item_metadata': [{'provenance': {'type': 'user'}}] if role == 'user' else []}}
        if role == 'assistant': payload.update(status='completed', phase='final_answer')
        payload.update(overrides)
        return {'timestamp': stamp, 'type': 'response_item', 'payload': payload}

    def write(self, rows):
        self.transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def capture(self):
        return delta_capture.capture_file(self.transcript, str(self.project), self.since,
                                          'org_private', self.states)

    def remote_rows(self):
        return json.loads(self.remote.read_text()).get('rows', {}) if self.remote.exists() else {}

    def test_captures_visible_conversation_privately_and_does_not_duplicate(self):
        result = self.capture()
        self.assertEqual(result['status'], 'locally_recorded', result)
        rows = self.remote_rows()
        self.assertEqual([row['update']['content'][0]['text'] for row in rows.values()],
                         ['Remember the final choice.', 'The final choice is blue.'])
        self.assertEqual([row['sequence'] for row in rows.values()], [1, 2])
        self.assertTrue(all('turnSequence' not in row for row in rows.values()))
        self.assertEqual(self.capture()['pending'], 0)
        self.assertEqual(self.remote_rows(), rows)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        create = next(row['args'] for row in calls if 'create' in row['args'])
        self.assertIn('--visibility', create)
        self.assertEqual(create[create.index('--visibility') + 1], 'private')
        self.assertNotIn('--worktree', create)

    def test_lost_ack_replays_identical_bytes_and_preserves_new_messages_for_later(self):
        with patch.dict(os.environ, CAPTURE_MODE='lost-ack'):
            result = self.capture()
        self.assertEqual(result['pending'], 2)
        self.assertEqual(result['acknowledged'], 0)
        self.assertEqual(len(self.remote_rows()), 2)
        with self.transcript.open('a') as stream:
            stream.write(json.dumps(self.message('a2', 'assistant', 'Another final choice.')) + '\n')
        self.assertEqual(self.capture()['pending'], 0)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        bodies = [call['body'] for call in calls if 'append' in call['args']]
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(len(self.remote_rows()), 2)
        self.assertEqual(self.capture()['captured'], 3)
        self.assertEqual(len(self.remote_rows()), 3)

    def test_partial_ack_retries_only_the_unacknowledged_suffix(self):
        with patch.dict(os.environ, CAPTURE_MODE='partial-ack'):
            result = self.capture()
        self.assertEqual(result['acknowledged'], 1)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(self.capture()['status'], 'locally_recorded')
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        bodies = [call['body'] for call in calls if 'append' in call['args']]
        self.assertEqual(bodies[1], bodies[0].splitlines(keepends=True)[1])
        self.assertEqual(len(self.remote_rows()), 2)

    def test_lost_create_response_retries_the_same_producer(self):
        with patch.dict(os.environ, CAPTURE_MODE='lost-create'):
            result = self.capture()
        self.assertEqual(result['pending'], 2)
        self.assertEqual(self.remote_rows(), {})
        self.assertEqual(self.capture()['status'], 'locally_recorded')
        calls = [json.loads(line)['args'] for line in self.calls.read_text().splitlines()]
        creates = [args for args in calls if 'create' in args]
        self.assertEqual(creates[0], creates[1])

    def test_future_only_excludes_harness_reasoning_tools_and_unfinalized_text(self):
        harness = self.message('h1', 'user', '# AGENTS.md instructions\nPRIVATE INSTRUCTIONS')
        harness['payload']['internal_chat_message_metadata_passthrough'].update(
            content_item_kinds=['agents_md.instructions'],
            content_item_metadata=[{'provenance': {'type': 'agents_md'}}])
        inherited = self.message('old2', 'user', 'inherited old history')
        inherited['payload']['internal_chat_message_metadata_passthrough']['create_time'] = 1
        self.write([self.meta(),
                    self.message('old', 'user', 'old history', stamp='2026-11-03T23:59:59Z'),
                    inherited, harness,
                    self.message('dev', 'developer', 'PRIVATE DEVELOPER'),
                    self.message('reasoning', 'assistant', 'PRIVATE THOUGHT', channel='analysis'),
                    self.message('partial', 'assistant', 'PARTIAL', status='in_progress'),
                    {'timestamp': self.since, 'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'DUPLICATE'}},
                    self.message('new', 'user', 'Visible user text')])
        result = self.capture()
        self.assertEqual(result['status'], 'locally_recorded', result)
        self.assertEqual([r['update']['content'][0]['text'] for r in self.remote_rows().values()], ['Visible user text'])

    def test_unknown_user_provenance_fails_closed_without_upload(self):
        row = self.message('u1', 'user', 'UNCLASSIFIED PRIVATE DATA')
        row['payload'].pop('internal_chat_message_metadata_passthrough')
        self.write([self.meta(), row])
        self.assertEqual(self.capture()['error'], 'unsupported_message_format')
        self.assertFalse(self.calls.exists())

    def test_subagent_metadata_cannot_be_overridden_by_later_parent_metadata(self):
        self.write([self.meta(source={'subagent': {'thread_spawn': {}}}, thread_source='subagent'),
                    self.meta(), self.message('u1', 'user', 'INHERITED')])
        self.assertEqual(self.capture()['status'], 'ignored')
        self.assertFalse(self.calls.exists())

    def test_different_project_is_ignored(self):
        self.write([self.meta(cwd=str(self.root)), self.message('u1', 'user', 'WRONG PROJECT')])
        self.assertEqual(self.capture()['status'], 'ignored')
        self.assertFalse(self.calls.exists())

    def test_partial_final_line_is_deferred_until_complete(self):
        row = json.dumps(self.message('u1', 'user', 'A complete message.'))
        self.transcript.write_text(json.dumps(self.meta()) + '\n' + row[:70])
        self.assertEqual(self.capture()['status'], 'idle')
        self.assertFalse(self.calls.exists())
        with self.transcript.open('a') as stream: stream.write(row[70:] + '\n')
        self.assertEqual(self.capture()['captured'], 1)

    def test_same_native_message_is_not_duplicated_but_changed_content_is_rejected(self):
        self.assertEqual(self.capture()['captured'], 2)
        with self.transcript.open('a') as stream:
            stream.write(json.dumps(self.message('u1', 'user', 'Remember the final choice.')) + '\n')
        self.assertEqual(self.capture()['captured'], 2)
        with self.transcript.open('a') as stream:
            stream.write(json.dumps(self.message('u1', 'user', 'CHANGED')) + '\n')
        self.assertEqual(self.capture()['error'], 'message_changed')
        self.assertEqual(len(self.remote_rows()), 2)

    def test_source_rewrite_or_truncation_does_not_reassign_sequences(self):
        self.capture()
        prior_calls = self.calls.read_bytes()
        self.write([self.meta()])
        self.assertEqual(self.capture()['error'], 'transcript_changed')
        self.assertEqual(self.calls.read_bytes(), prior_calls)

    def test_missing_or_corrupt_state_cannot_reuse_an_existing_producer(self):
        self.capture()
        state = self.states / (self.session + '.json')
        state.unlink()
        result = self.capture()
        self.assertEqual(result['error'], 'producer_recovery_required', result)
        prior = self.calls.read_bytes()
        state.write_text('{broken')
        self.assertEqual(self.capture()['error'], 'invalid_capture_state')
        self.assertEqual(self.calls.read_bytes(), prior)

    def test_remote_files_or_wrong_identity_prevent_any_append(self):
        for mode in ('remote-files', 'wrong-identity'):
            with self.subTest(mode=mode), patch.dict(os.environ, CAPTURE_MODE=mode):
                result = self.capture()
                self.assertEqual(result['status'], 'error')
                self.assertEqual(self.remote_rows(), {})

    def test_bad_receipt_does_not_advance_checkpoint(self):
        with patch.dict(os.environ, CAPTURE_MODE='bad-receipt'):
            result = self.capture()
        self.assertEqual(result['error'], 'invalid_acknowledgement')
        self.assertEqual(result['acknowledged'], 0)
        self.assertEqual(result['pending'], 2)
        self.assertEqual(self.capture()['status'], 'locally_recorded')

    def test_incompatible_contract_keeps_private_outbox_without_creating_thread(self):
        with patch.dict(os.environ, CAPTURE_MODE='old-contract'):
            result = self.capture()
        self.assertEqual(result['error'], 'unsupported_contract')
        self.assertEqual(result['pending'], 2)
        self.assertFalse(self.remote.exists())
        state = self.states / (self.session + '.json')
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_transition_to_another_workspace_is_not_uploaded(self):
        self.write([self.meta(), {'type': 'turn_context', 'payload': {'cwd': str(self.root)}},
                    self.message('x', 'user', 'OTHER WORKSPACE'),
                    {'type': 'turn_context', 'payload': {'cwd': str(self.project)}},
                    self.message('u1', 'user', 'OUR WORKSPACE')])
        self.assertEqual(self.capture()['captured'], 1)
        self.assertEqual(next(iter(self.remote_rows().values()))['update']['content'][0]['text'], 'OUR WORKSPACE')

    def test_bounded_batch_exposes_backlog_until_all_complete_lines_are_captured(self):
        with patch.object(delta_capture, 'MAX_RECORDS', 1):
            result = self.capture()
            self.assertTrue(result['backlog'])
            self.assertEqual(result['status'], 'pending')
            result = self.capture()
            self.assertFalse(result['backlog'])
            self.assertEqual(result['captured'], 2)
        with self.transcript.open('a') as stream: stream.write('{"partial":')
        self.assertFalse(self.capture()['backlog'])

    def test_changed_pending_bytes_are_rejected_before_a_retry(self):
        with patch.dict(os.environ, CAPTURE_MODE='lost-ack'): self.capture()
        path = self.states / (self.session + '.json')
        state = json.loads(path.read_text())
        state['pending'][0] = state['pending'][0].replace('Remember', 'CHANGED')
        path.write_text(json.dumps(state))
        before = self.calls.read_bytes()
        self.assertEqual(self.capture()['error'], 'invalid_capture_state')
        self.assertEqual(self.calls.read_bytes(), before)

    def test_typed_retry_hint_is_exposed_without_provider_message(self):
        with patch.dict(os.environ, CAPTURE_MODE='retryable'):
            result = self.capture()
        self.assertEqual(result['error'], 'app_unavailable')
        self.assertTrue(result['retryable'])
        self.assertEqual(result['retry_after_ms'], 15000)
        self.assertNotIn('PRIVATE PROVIDER MESSAGE', json.dumps(result))

    def test_nonowner_access_or_untrusted_url_cannot_receive_messages(self):
        for mode in ('nonowner', 'unsafe-url'):
            with self.subTest(mode=mode), patch.dict(os.environ, CAPTURE_MODE=mode):
                result = self.capture()
                self.assertEqual(result['status'], 'error')
                self.assertEqual(self.remote_rows(), {})

    def test_timeout_retains_only_unacknowledged_records_and_can_resume(self):
        execute = subprocess.run
        def timeout_after_delivery(args, **kwargs):
            result = execute(args, **kwargs)
            if 'append' in args:
                raise subprocess.TimeoutExpired(args, 20, output=(result.stdout.splitlines()[0] + '\n').encode())
            return result
        with patch.object(delta_capture.subprocess, 'run', side_effect=timeout_after_delivery):
            result = self.capture()
        self.assertEqual(result['error'], 'timeout')
        self.assertTrue(result['retryable'])
        self.assertEqual(result['acknowledged'], 1)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(self.capture()['status'], 'locally_recorded')
        self.assertEqual(len(self.remote_rows()), 2)

    def test_concurrent_observers_do_not_duplicate_records(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.capture(), range(2)))
        self.assertTrue(all(result['status'] == 'locally_recorded' for result in results), results)
        self.assertEqual(len(self.remote_rows()), 2)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(sum('append' in call['args'] for call in calls), 1)

    def test_corrupt_provenance_or_oversized_message_fail_without_network(self):
        row = self.message('u1', 'user', 'real user text')
        row['payload']['internal_chat_message_metadata_passthrough']['content_item_metadata'][0]['provenance'] = None
        self.write([self.meta(), row])
        self.assertEqual(self.capture()['error'], 'unsupported_message_format')
        self.assertFalse(self.calls.exists())
        self.write([self.meta(), self.message('u2', 'user', 'x' * 40)])
        with patch.object(delta_capture, 'MAX_MESSAGE', 32):
            self.assertEqual(self.capture()['error'], 'message_too_large')
        self.assertFalse(self.calls.exists())

    def test_file_replacement_and_symlinks_cannot_reuse_old_state(self):
        self.capture()
        before = self.calls.read_bytes()
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.transcript.read_bytes())
        replacement.replace(self.transcript)
        self.assertEqual(self.capture()['error'], 'invalid_capture_state')
        self.assertEqual(self.calls.read_bytes(), before)
        self.transcript.rename(replacement)
        self.transcript.symlink_to(replacement)
        self.assertEqual(self.capture()['error'], 'unsafe_path')

    def test_terminal_error_after_ack_keeps_upstream_retry_hint(self):
        with patch.dict(os.environ, CAPTURE_MODE='partial-error'):
            result = self.capture()
        self.assertEqual(result['acknowledged'], 1)
        self.assertEqual(result['error'], 'app_unavailable')
        self.assertTrue(result['retryable'])
        self.assertEqual(result['retry_after_ms'], 4000)

    def test_git_subdirectory_matches_project_but_nested_repository_does_not(self):
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        subdirectory = self.project / 'src'
        subdirectory.mkdir()
        self.write([self.meta(cwd=str(subdirectory)),
                    {'type': 'turn_context', 'payload': {'cwd': str(subdirectory)}},
                    self.message('u1', 'user', 'Same repository')])
        self.assertEqual(self.capture()['status'], 'locally_recorded')
        nested = self.project / 'nested'
        nested.mkdir()
        subprocess.run(['git', 'init', '-q', str(nested)], check=True)
        self.write([self.meta(cwd=str(nested)), self.message('u2', 'user', 'Nested repository')])
        self.assertEqual(self.capture()['status'], 'ignored')


FAKE_DELTA = textwrap.dedent(r'''
import json, os, pathlib, sys
args = sys.argv[1:]
body = sys.stdin.read() if 'append' in args else ''
with open(os.environ['CAPTURE_CALLS'], 'a') as f: f.write(json.dumps({'args': args, 'body': body}) + '\n')
path = pathlib.Path(os.environ['CAPTURE_REMOTE'])
data = json.loads(path.read_text()) if path.exists() else {'rows': {}}
mode = os.environ.get('CAPTURE_MODE', '')
def emit(row, code=0):
    print(json.dumps(row), flush=True)
    sys.exit(code)
def save(): path.write_text(json.dumps(data))
command = args[2]
if mode == 'retryable': emit({'error': {'code': 'app_unavailable', 'retryable': True, 'retry_after_ms': 15000, 'message': 'PRIVATE PROVIDER MESSAGE'}}, 1)
if command == 'contract': emit({'operation': 'contract', 'min': 7 if mode == 'old-contract' else 8, 'max': 7 if mode == 'old-contract' else 8})
if command == 'create':
    data['external_id'] = args[args.index('--external-id') + 1]
    save()
    if mode == 'lost-create': sys.exit(1)
    emit({'operation': 'create', 'thread_id': 'thread_capture', 'source': 'honey-codex', 'external_id': data['external_id']})
if command == 'status':
    emit({'operation': 'status', 'thread_id': 'thread_capture', 'visibility': 'private',
          'url': 'https://evil.example/thread' if mode == 'unsafe-url' else 'https://delta.dev/join/thread_capture',
          'upload': {'state': 'uploaded', 'organization_id': 'org_private'},
          'access': {'state': 'granted', 'is_owner': mode != 'nonowner'},
          'worktrees': {'status': 'available', 'items': [{}] if mode == 'remote-files' else []},
          'producer': {'source': 'wrong' if mode == 'wrong-identity' else 'honey-codex', 'external_id': data['external_id'], 'next_sequence': len(data['rows']) + 1}})
if command == 'append':
    for line in body.splitlines():
        row = json.loads(line); key = str(row['sequence'])
        duplicate = key in data['rows']
        if duplicate and data['rows'][key] != row: emit({'error': {'code': 'sequence_conflict'}}, 1)
        data['rows'][key] = row; save()
        if mode == 'lost-ack': continue
        print(json.dumps({'operation': 'appended', 'thread_id': 'WRONG' if mode == 'bad-receipt' else 'thread_capture', 'sequence': row['sequence'],
                          'next_sequence': len(data['rows']) + 1, 'duplicate': duplicate,
                          'disposition': 'duplicate' if duplicate else 'recorded', 'turn_sequence': None, 'version': 'opaque-v1'}), flush=True)
        if mode == 'partial-ack': sys.exit(1)
        if mode == 'partial-error': emit({'error': {'code': 'app_unavailable', 'retryable': True, 'retry_after_ms': 4000}}, 1)
    if mode == 'lost-ack': sys.exit(1)
    sys.exit(0)
emit({'error': {'code': 'unsupported'}}, 1)
''')


if __name__ == '__main__':
    unittest.main()
