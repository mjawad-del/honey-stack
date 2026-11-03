"""Bounded Codex transcript capture with an immutable, acknowledged Delta outbox.

Only explicit user-origin text and completed visible assistant messages are sent.
This observer is independent of native hooks and never runs a remote agent turn.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from honey.rsi import store

SOURCE = 'honey-codex'
MAX_FILE = 128 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024
MAX_SCAN = 4 * 1024 * 1024
MAX_MESSAGE = 128 * 1024
MAX_BATCH = 512 * 1024
MAX_RECORDS = 100
MAX_STATE = 16 * 1024 * 1024
UUID = re.compile(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}')


class CaptureError(Exception):
    def __init__(self, code, retryable=False, retry_after_ms=None):
        super().__init__(code)
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms


def _timestamp(value):
    if not isinstance(value, str):
        raise CaptureError('invalid_timestamp')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
            raise ValueError()
        return result.timestamp()
    except (ValueError, OverflowError):
        raise CaptureError('invalid_timestamp') from None


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _save(path, state):
    """Replace and fsync before any network mutation can consume the outbox."""
    data = (json.dumps(state, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    if len(data) > MAX_STATE:
        raise CaptureError('capture_state_too_large')
    if path.is_symlink():
        raise CaptureError('unsafe_state')
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.capture-', dir=str(path.parent))
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise CaptureError('capture_state_write_failed') from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _json(raw):
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError, TypeError):
        raise CaptureError('invalid_response') from None


def _command(*args, body=None):
    executable = shutil.which('delta')
    if not executable:
        raise CaptureError('not_installed')
    try:
        result = subprocess.run([executable, '--json', 'thread', *args], input=body,
                                text=True, capture_output=True, timeout=20,
                                **({'stdin': subprocess.DEVNULL} if body is None else {}))
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ''
        if isinstance(output, bytes):
            output = output.decode('utf-8', 'replace')
        return output, CaptureError('timeout', True)
    except (OSError, UnicodeError):
        raise CaptureError('delta_unavailable') from None
    error = None
    if result.returncode:
        error = CaptureError('delta_command_failed')
        for line in reversed(result.stdout.splitlines()):
            try:
                failure = _json(line).get('error', {})
                code = failure.get('code')
                if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,80}', code):
                    delay = failure.get('retry_after_ms')
                    delay = delay if type(delay) is int and 0 <= delay <= 3600000 else None
                    error = CaptureError(code, failure.get('retryable') is True, delay)
                    break
            except (CaptureError, AttributeError):
                pass
    return result.stdout, error


def _request(*args):
    output, error = _command(*args)
    if error:
        raise error
    result = _json(output)
    if result.get('error') is not None:
        raise CaptureError('delta_command_failed')
    return result


def _prefix(stream, length):
    stream.seek(0)
    digest = hashlib.sha256()
    while length:
        chunk = stream.read(min(length, MAX_SCAN))
        if not chunk:
            raise CaptureError('transcript_changed')
        digest.update(chunk)
        length -= len(chunk)
    return digest.hexdigest()


def _project_cwd(value, project, cache):
    if not isinstance(value, str) or not os.path.isabs(value):
        raise CaptureError('invalid_transcript')
    path = str(Path(value).resolve())
    if path == project:
        return project
    if path not in cache:
        from honey import delta
        try:
            cache[path] = str(delta.project_root(path))
        except delta.DeltaError:
            raise CaptureError('invalid_transcript') from None
    return cache[path]


def _metadata(stream, transcript, project, cache):
    line = stream.readline(MAX_LINE + 1)
    if not line.endswith(b'\n') or len(line) > MAX_LINE:
        raise CaptureError('incomplete_metadata')
    try:
        row = _json(line)
        meta = row['payload']
        ident = meta['id']
        if row.get('type') != 'session_meta' or not isinstance(meta, dict):
            raise ValueError()
        if not isinstance(ident, str) or not UUID.fullmatch(ident):
            raise ValueError()
        if not transcript.name.endswith('-' + ident + '.jsonl'):
            raise ValueError()
        if meta.get('parent_thread_id') or meta.get('forked_from_id') or meta.get('thread_source'):
            raise CaptureError('subagent_or_fork')
        if meta.get('source') not in ('cli', 'vscode', 'exec', 'app-server'):
            raise CaptureError('unsupported_session_source')
        if meta.get('session_id', ident) != ident:
            raise CaptureError('session_identity_mismatch')
        if _project_cwd(meta.get('cwd'), project, cache) != project:
            raise CaptureError('different_project')
        return ident
    except (KeyError, TypeError, ValueError):
        raise CaptureError('invalid_metadata') from None


def _text(row, since):
    """Return (native id, role, text), or None for explicitly excluded records."""
    if row.get('type') != 'response_item':
        return None
    message = row.get('payload')
    if not isinstance(message, dict) or message.get('type') != 'message':
        return None
    role = message.get('role')
    if role not in ('user', 'assistant'):
        return None
    if _timestamp(row.get('timestamp')) < since:
        return None
    metadata = message.get('internal_chat_message_metadata_passthrough')
    if not isinstance(metadata, dict):
        raise CaptureError('unsupported_message_format')
    created = metadata.get('create_time')
    if type(created) not in (int, float) or not math.isfinite(created):
        raise CaptureError('unsupported_message_format')
    if created < since:
        return None
    content = message.get('content')
    if not isinstance(content, list):
        raise CaptureError('unsupported_message_format')
    if role == 'assistant':
        if message.get('channel') in ('analysis', 'summary') or message.get('phase') == 'analysis':
            return None
        phase = message.get('phase') or message.get('channel')
        if message.get('status') != 'completed':
            return None
        if phase not in ('commentary', 'final', 'final_answer'):
            raise CaptureError('unsupported_message_format')
        selected = content
        kind = 'output_text'
    else:
        kinds, provenance = metadata.get('content_item_kinds'), metadata.get('content_item_metadata')
        if not isinstance(kinds, list) or len(kinds) != len(content):
            raise CaptureError('unsupported_message_format')
        if not any(kind == 'user.text' for kind in kinds):
            # Known harness input is excluded, unknown classification is visible.
            if any(kind == 'unknown' or not isinstance(kind, str) for kind in kinds):
                raise CaptureError('unsupported_message_format')
            return None
        if not isinstance(provenance, list) or len(provenance) != len(content):
            raise CaptureError('unsupported_message_format')
        selected = []
        for index, kind in enumerate(kinds):
            if kind != 'user.text':
                continue
            item = provenance[index]
            origin = item.get('provenance') if isinstance(item, dict) else None
            if not isinstance(origin, dict) or origin.get('type') != 'user':
                raise CaptureError('unsupported_message_format')
            selected.append(content[index])
        kind = 'input_text'
    if any(not isinstance(item, dict) or item.get('type') != kind or not isinstance(item.get('text'), str)
           for item in selected):
        raise CaptureError('unsupported_message_format')
    value = '\n'.join(item['text'] for item in selected)
    if not value.strip():
        return None
    if role == 'user' and value.lstrip().startswith(('# AGENTS.md instructions', '<environment_context>',
                                                   'Context handoff', '<INSTRUCTIONS>')):
        return None
    ident = message.get('id')
    if not isinstance(ident, str) or not ident or len(ident) > 256:
        raise CaptureError('unsupported_message_format')
    if len(value.encode()) > MAX_MESSAGE:
        raise CaptureError('message_too_large')
    return ident, role, value


def _scan(stream, state, cache):
    next_state = copy.deepcopy(state)
    stream.seek(state['cursor'])
    start = stream.tell()
    size = 0
    since = _timestamp(state['since'])
    while stream.tell() - start < MAX_SCAN and len(next_state['pending']) < MAX_RECORDS:
        position = stream.tell()
        line = stream.readline(MAX_LINE + 1)
        if not line or not line.endswith(b'\n'):
            if len(line) > MAX_LINE:
                raise CaptureError('transcript_line_too_large')
            break
        if len(line) > MAX_LINE:
            raise CaptureError('transcript_line_too_large')
        try:
            row = _json(line)
        except CaptureError:
            raise CaptureError('invalid_transcript') from None
        if row.get('type') == 'session_meta' and position != 0:
            raise CaptureError('repeated_metadata')
        if row.get('type') == 'turn_context':
            payload = row.get('payload')
            if not isinstance(payload, dict) or not isinstance(payload.get('cwd'), str):
                raise CaptureError('invalid_transcript')
            next_state['current_cwd'] = _project_cwd(payload['cwd'], state['project'], cache)
        record = _text(row, since) if next_state['current_cwd'] == state['project'] else None
        if record:
            native_id, role, value = record
            identity = _digest(native_id.encode())
            checksum = _digest((role + '\0' + value).encode())
            prior = next_state['seen'].get(identity)
            if prior is not None and prior != checksum:
                raise CaptureError('message_changed')
            if prior is None:
                update = {'schemaVersion': 1, 'sequence': next_state['next_sequence'], 'update': {
                    'sessionUpdate': 'user_message' if role == 'user' else 'agent_message',
                    'messageId': 'codex-' + _digest((state['external_id'] + '\0' + native_id).encode())[:40],
                    'content': [{'type': 'text', 'text': value}]}}
                encoded = json.dumps(update, ensure_ascii=False, separators=(',', ':'))
                if size + len(encoded.encode()) > MAX_BATCH:
                    break
                size += len(encoded.encode())
                next_state['pending'].append(encoded)
                next_state['pending_digests'].append(_digest(encoded.encode()))
                next_state['next_sequence'] += 1
                next_state['seen'][identity] = checksum
        next_state['cursor'] = stream.tell()
    next_state['prefix'] = _prefix(stream, next_state['cursor'])
    next_state['backlog'] = _backlog(stream, next_state['cursor'])
    return next_state


def _backlog(stream, cursor):
    stream.seek(cursor)
    line = stream.readline(MAX_LINE + 1)
    return bool(line.endswith(b'\n') or len(line) > MAX_LINE)


def _load(path, initial):
    if not path.exists():
        return initial
    if path.is_symlink() or path.stat().st_size > MAX_STATE:
        raise CaptureError('unsafe_state')
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            raise ValueError()
        for key in ('version', 'project', 'since', 'organization', 'external_id', 'native_session_id',
                    'transcript', 'file_id'):
            if state.get(key) != initial[key]:
                raise ValueError()
        for key in ('cursor', 'next_sequence', 'acknowledged'):
            if type(state.get(key)) is not int or state[key] < (1 if key == 'next_sequence' else 0):
                raise ValueError()
        if not isinstance(state.get('prefix'), str) or not re.fullmatch(r'[a-f0-9]{64}', state['prefix']):
            raise ValueError()
        if (not isinstance(state.get('seen'), dict) or not isinstance(state.get('pending'), list)
                or not isinstance(state.get('pending_digests'), list) or type(state.get('backlog')) is not bool):
            raise ValueError()
        if (not isinstance(state.get('current_cwd'), str) or not isinstance(state.get('cloud'), dict)
                or type(state.get('producer_initialized')) is not bool):
            raise ValueError()
        if state.get('thread') is not None and not isinstance(state['thread'], str):
            raise ValueError()
        if len(state['pending']) > MAX_RECORDS or state['acknowledged'] + len(state['pending']) + 1 != state['next_sequence']:
            raise ValueError()
        if len(state['pending_digests']) != len(state['pending']) or len(state['seen']) != state['next_sequence'] - 1:
            raise ValueError()
        if any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r'[a-f0-9]{64}', k)
               or not re.fullmatch(r'[a-f0-9]{64}', v) for k, v in state['seen'].items()):
            raise ValueError()
        for index, raw in enumerate(state['pending']):
            seq = state['acknowledged'] + index + 1
            if not isinstance(raw, str) or _digest(raw.encode()) != state['pending_digests'][index]:
                raise ValueError()
            record = json.loads(raw)
            if (type(record.get('sequence')) is not int or record['sequence'] != seq
                    or record.get('schemaVersion') != 1 or 'turnSequence' in record):
                raise ValueError()
            update = record['update']
            if (update['sessionUpdate'] not in ('user_message', 'agent_message')
                    or not re.fullmatch(r'codex-[a-f0-9]{40}', update['messageId'])
                    or len(update['content']) != 1 or update['content'][0]['type'] != 'text'
                    or not isinstance(update['content'][0]['text'], str)):
                raise ValueError()
        return state
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise CaptureError('invalid_capture_state') from None


def _status(state):
    result = _request('status', state['thread'], '--source', SOURCE, '--external-id', state['external_id'])
    producer, trees, upload = result.get('producer'), result.get('worktrees'), result.get('upload')
    access, url = result.get('access'), result.get('url')
    if (result.get('operation') != 'status' or result.get('thread_id') != state['thread']
            or not isinstance(producer, dict) or producer.get('source') != SOURCE
            or producer.get('external_id') != state['external_id']):
        raise CaptureError('producer_identity_mismatch')
    number = producer.get('next_sequence')
    if not state['producer_initialized'] and number != 1:
        raise CaptureError('producer_recovery_required')
    if type(number) is not int or not state['acknowledged'] + 1 <= number <= state['next_sequence']:
        raise CaptureError('producer_sequence_mismatch')
    if (result.get('visibility') not in (None, 'private') or not isinstance(trees, dict)
            or trees.get('status') != 'available' or trees.get('items') != []
            or not isinstance(upload, dict) or upload.get('organization_id') != state['organization']):
        raise CaptureError('unsafe_remote_thread')
    if (not isinstance(access, dict) or access.get('state') not in ('local_only', 'granted')
            or (access.get('state') == 'granted' and access.get('is_owner') is not True)
            or (access.get('state') == 'local_only' and access.get('is_owner') is not None
                and access.get('is_owner') is not True)):
        raise CaptureError('remote_access_not_owned')
    if url is not None and (not isinstance(url, str) or not re.fullmatch(
            r'https://delta\.dev/join/[A-Za-z0-9][A-Za-z0-9_-]{1,127}', url)):
        raise CaptureError('invalid_share_url')
    cloud_state = upload.get('state')
    if cloud_state not in ('queued', 'uploading', 'uploaded', 'failed'):
        raise CaptureError('invalid_cloud_status')
    state['cloud'] = {'state': cloud_state, 'url': result.get('url'), 'visibility': result.get('visibility')}
    if cloud_state == 'failed':
        raise CaptureError('cloud_upload_failed')


def _flush(path, state):
    contract = _request('contract')
    if (contract.get('operation') != 'contract' or type(contract.get('min')) is not int
            or type(contract.get('max')) is not int or not contract['min'] <= 8 <= contract['max']):
        raise CaptureError('unsupported_contract')
    if not state['thread']:
        created = _request('create', '--source', SOURCE, '--external-id', state['external_id'],
                           '--organization', state['organization'], '--visibility', 'private')
        ident = created.get('thread_id')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{1,127}', ident):
            raise CaptureError('invalid_create_response')
        state['thread'] = ident
        _save(path, state)
    _status(state)
    if not state['producer_initialized']:
        state['producer_initialized'] = True
        _save(path, state)
    if state['pending']:
        output, error = _command('append', state['thread'], '--source', SOURCE, '--external-id', state['external_id'],
                                 '--producer-name', 'Honey Codex capture', '--acknowledge', 'local',
                                 body=''.join(raw + '\n' for raw in state['pending']))
        acknowledged = 0
        for line in output.splitlines():
            try:
                receipt = _json(line)
                seq = state['acknowledged'] + acknowledged + 1
                number = receipt.get('next_sequence')
                disposition = receipt.get('disposition')
                if (acknowledged >= len(state['pending']) or receipt.get('operation') != 'appended'
                        or receipt.get('thread_id') != state['thread'] or type(receipt.get('sequence')) is not int
                        or receipt['sequence'] != seq
                        or type(number) is not int or not seq < number <= state['next_sequence']
                        or not isinstance(receipt.get('version'), str) or not receipt['version'].strip()
                        or type(receipt.get('duplicate')) is not bool or receipt.get('turn_sequence', 'missing') is not None
                        or disposition not in ('recorded', 'duplicate')
                        or receipt['duplicate'] != (disposition == 'duplicate')):
                    raise CaptureError('invalid_acknowledgement')
                acknowledged += 1
            except CaptureError:
                error = error or CaptureError('invalid_acknowledgement')
                break
        state['acknowledged'] += acknowledged
        state['pending'] = state['pending'][acknowledged:]
        state['pending_digests'] = state['pending_digests'][acknowledged:]
        _save(path, state)
        if error or state['pending']:
            raise error or CaptureError('incomplete_acknowledgement', True)
        _status(state)
    _save(path, state)


def _result(state=None, error=None, ignored=False):
    state = state or {}
    pending = len(state.get('pending', []))
    cloud = state.get('cloud', {'state': 'unverified', 'url': None})
    return {'status': 'ignored' if ignored else 'error' if error else 'pending' if pending or state.get('backlog') or (
                state.get('thread') and cloud.get('state') != 'uploaded') else 'locally_recorded' if state.get('thread') else 'idle',
            'source': SOURCE, 'native_session_id': state.get('native_session_id'),
            'external_id': state.get('external_id'), 'thread': state.get('thread'), 'cloud': cloud,
            'error': str(error) if error else None, 'retryable': getattr(error, 'retryable', False),
            'retry_after_ms': getattr(error, 'retry_after_ms', None), 'backlog': state.get('backlog', False),
            'pending': pending, 'captured': state.get('next_sequence', 1) - 1,
            'acknowledged': state.get('acknowledged', 0),
            'acknowledgement': 'local' if state.get('acknowledged', 0) else None}


def capture_file(transcript: Path, project: str, since: str, organization: str, state_dir: Path) -> dict:
    """Capture one bounded batch, preserving exact pending bytes across every retry."""
    state = None
    try:
        _timestamp(since)
        if not isinstance(organization, str) or not re.fullmatch(r'org_[A-Za-z0-9_-]{1,120}', organization):
            raise CaptureError('invalid_organization')
        project = str(Path(project).resolve())
        transcript, directory = Path(transcript), Path(state_dir)
        if transcript.is_symlink() or directory.is_symlink():
            raise CaptureError('unsafe_path')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with transcript.open('rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                raise CaptureError('unsupported_transcript_size')
            cache = {}
            ident = _metadata(stream, transcript, project, cache)
            external = ident + ':' + _digest(since.encode())[:12]
            path = directory / (ident + '.json')
            initial = {'version': 1, 'project': project, 'since': since, 'organization': organization,
                       'native_session_id': ident, 'external_id': external, 'transcript': str(transcript.resolve()),
                       'file_id': [info.st_dev, info.st_ino], 'cursor': 0, 'prefix': _digest(b''),
                       'current_cwd': project, 'next_sequence': 1, 'acknowledged': 0,
                       'pending': [], 'pending_digests': [], 'backlog': False, 'seen': {},
                       'thread': None, 'cloud': {'state': 'unverified', 'url': None},
                       'error': None, 'producer_initialized': False}
            if path.with_suffix('.json.lock').is_symlink():
                raise CaptureError('unsafe_state')
            with store.session_lock(str(path)):
                state = _load(path, initial)
                if info.st_size < state['cursor'] or _prefix(stream, state['cursor']) != state['prefix']:
                    raise CaptureError('transcript_changed')
                if not state['pending']:
                    state = _scan(stream, state, cache)
                else:
                    state['backlog'] = _backlog(stream, state['cursor'])
                state['error'] = None
                _save(path, state)
                try:
                    if state['pending'] or state['thread']:
                        _flush(path, state)
                except CaptureError as exc:
                    state['error'] = str(exc)
                    state['retryable'] = exc.retryable
                    state['retry_after_ms'] = exc.retry_after_ms
                    _save(path, state)
                    return _result(state, exc)
                return _result(state)
    except CaptureError as exc:
        return _result(state, exc, str(exc) in ('different_project', 'subagent_or_fork'))
    except (OSError, TypeError, ValueError):
        return _result(state, 'capture_io_error')
