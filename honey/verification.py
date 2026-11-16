"""Run a real check and bind its result to a Git working-tree snapshot."""
from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import time

from honey import filelock
from honey.rsi import store, text


CONTENT_LIMIT = 64 * 1024 * 1024  # beyond this, files are tracked by size and timestamps only


def _status_paths(status: bytes) -> list:
    """Worktree-side paths from `git status --porcelain=v2 -z` (changed, renamed, unmerged, untracked)."""
    paths, entries, i = [], status.split(b'\0'), 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if entry[:2] == b'1 ':
            paths.append(entry.split(b' ', 8)[8])
        elif entry[:2] == b'2 ':
            paths.append(entry.split(b' ', 9)[9])
            i += 1  # the original path of the rename/copy follows as its own entry
        elif entry[:2] == b'u ':
            paths.append(entry.split(b' ', 10)[10])
        elif entry[:2] == b'? ':
            paths.append(entry[2:])
    return paths


def snapshot(cwd):
    """Digest of the Git working tree that contains cwd (worktrees and subfolders included).

    HEAD, the index (blob ids) and `git status` pin every clean file; only files git reports as changed or
    untracked are hashed here. git's stat cache includes ctime, which writes always move, so it is the
    same check `git status` makes. Read-only: --no-optional-locks keeps git from refreshing .git/index.
    Windows ctime is creation time (writes don't move it), so there every tracked and untracked file is hashed.
    Symlinks are hashed by their target path, not followed. Past CONTENT_LIMIT bytes of changed files,
    files contribute metadata (size, mtime, ctime) instead of content.
    """
    def git(where, *args):
        return subprocess.run(['git', '--no-optional-locks', '-C', where, *args], capture_output=True, check=True).stdout
    root = os.path.realpath(os.fsdecode(git(cwd, 'rev-parse', '--show-toplevel')).strip())
    status = git(root, 'status', '--porcelain=v2', '-z', '--untracked-files=all', '--ignore-submodules=none')
    digest = hashlib.sha256(git(root, 'rev-parse', 'HEAD') + git(root, 'ls-files', '--stage', '-z') + status)
    total = 0
    if os.name == 'nt':
        names = set(git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0')) - {b''}
    else:
        names = set(_status_paths(status))
    for name in sorted(names):
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
    with filelock.exclusive(file + '.verification.lock'):
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
