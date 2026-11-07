"""Run a real check and bind its result to a Git working-tree snapshot."""
from __future__ import annotations

import fcntl
import hashlib
import os
import shlex
import subprocess
import time

from honey.rsi import store, text


CONTENT_LIMIT = 64 * 1024 * 1024  # beyond this, files are tracked by size and timestamps only


def snapshot(cwd):
    """Digest of the Git working tree that contains cwd (worktrees and subfolders included).

    Symlinks are hashed by their target path, not followed. Large trees still get a digest:
    after CONTENT_LIMIT bytes, files contribute metadata (size, mtime, ctime) instead of content.
    """
    def git(where, *args):
        return subprocess.run(['git', '-C', where, *args], capture_output=True, check=True).stdout
    root = os.path.realpath(os.fsdecode(git(cwd, 'rev-parse', '--show-toplevel')).strip())
    digest = hashlib.sha256(git(root, 'rev-parse', 'HEAD') + git(root, 'ls-files', '--stage', '-z'))
    names = sorted(set(git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0')) - {b''})
    total = 0
    for name in names:
        path = os.path.join(root, os.fsdecode(name))
        digest.update(name + b'\0')
        if os.path.islink(path):
            digest.update(b'link:' + os.fsencode(os.readlink(path)) + b'\0')
            continue
        if not os.path.isfile(path):
            digest.update(b'deleted\0')
            continue
        stat = os.stat(path)
        digest.update(f'{stat.st_mode}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'.encode() + b'\0')
        total += stat.st_size
        if total > CONTENT_LIMIT:
            continue
        with open(path, 'rb') as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        digest.update(b'\0')
    return digest.hexdigest()


def valid(task, proof):
    if proof.get('source') != 'runner' or not proof.get('success'):
        return False
    try:
        return proof.get('snapshot') == snapshot(task['cwd'])
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def run(file, check, argv):
    from honey import tasks
    if not argv or tasks.command_kind(shlex.join(argv)) != 'check':
        raise ValueError('task run requires a recognized test, lint, typecheck or diff-check command')
    fd = os.open(file + '.verification.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with store.session_lock(file):
            state = store.read_json(file, {}) or {}
            task = state.get('task') or {}
            if task.get('mode') != 'execute' or check not in task.get('checks', []):
                raise ValueError('Choose a required check from an execution task')
            before = snapshot(task['cwd'])
            ident = task['id']
            command = shlex.join(argv)
            fingerprint = hashlib.sha256(command.encode()).hexdigest()
            tasks._outcome(task, fingerprint, 'unknown')
            store.write_json(file, state)
        started = time.monotonic()
        try:
            completed = subprocess.run(argv, cwd=task['cwd'], timeout=900)
            code = completed.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            code = 1
            print('Verification command failed: ' + str(exc))
        with store.session_lock(file):
            state = store.read_json(file, {}) or {}
            task = state.get('task') or {}
            if task.get('id') != ident:
                raise ValueError('Task changed while verification was running; result not applied')
            success = code == 0 and before == snapshot(task['cwd'])
            proof = {'id': 'run-' + str(time.time_ns()), 'source': 'runner', 'snapshot': before,
                     'success': success, 'exit_code': code, 'mutation': task['mutation'], 'fingerprint': fingerprint,
                     'command': text.redact(command, 160), 'cwd': task['cwd'],
                     'duration_ms': round((time.monotonic() - started) * 1000)}
            tasks._outcome(task, fingerprint, 'passed' if success else 'failed')
            task['calls'] = (task.get('calls', []) + [proof])[-40:]
            if success:
                task['evidence'][check] = proof
            store.write_json(file, state)
            print(('Verification passed: ' if success else 'Verification failed or source changed: ') + proof['id'])
            return 0 if success else 1
