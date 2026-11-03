"""Opted-in Codex transcript observer, independent of native hook delivery."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time

from honey import delta, paths
from honey.rsi import store


LABEL = 'dev.honey.delta-capture'
INTERVAL = 15


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _root():
    return Path(paths.path('delta'))


def _read(file, default):
    try:
        if file.is_symlink() or file.stat().st_size > 8 * 1024 * 1024:
            raise ValueError()
        result = json.loads(file.read_text())
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        raise delta.DeltaError('invalid_capture_state', 'Repair the local capture state before continuing.') from None


def _save(file, value):
    _root().mkdir(parents=True, exist_ok=True, mode=0o700)
    delta.write_private(file, json.dumps(value, ensure_ascii=False) + '\n')


def _policies():
    result = _read(_root() / 'projects.json', {'version': 1, 'projects': {}})
    if result.get('version') != 1 or not isinstance(result.get('projects'), dict):
        raise delta.DeltaError('invalid_capture_settings', 'Repair the capture project registry before continuing.')
    for project, value in result['projects'].items():
        try:
            if (not Path(project).is_absolute() or not isinstance(value, dict)
                    or type(value.get('enabled')) is not bool
                    or not re.fullmatch(r'org_[A-Za-z0-9_-]{1,120}', value.get('organization', ''))):
                raise ValueError()
            _since(value['since'])
        except (ValueError, TypeError, KeyError):
            raise delta.DeltaError('invalid_capture_settings', 'Repair the capture project registry before continuing.') from None
    return result


def _since(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be a string')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp must include timezone')
    return parsed.timestamp()


def _project(project=None, must_exist=False):
    directory = Path(project or os.getcwd()).resolve()
    return str(delta.project_root(directory)) if directory.is_dir() or must_exist else str(directory)


def project_status(project=None):
    root = _project(project)
    value = _policies()['projects'].get(root, {})
    return {'project': root, 'configured': bool(value), 'enabled': value.get('enabled', False),
            'path': str(_root() / 'projects.json'),
            'since': value.get('since'), 'organization': value.get('organization'),
            'mode': 'private' if value.get('enabled') else 'manual',
            'content': 'visible user and assistant text', 'historical_backfill': False}


def configure(project, organization, enabled):
    root = _project(project, must_exist=enabled)
    with store.session_lock(str(_root() / 'observer')):
        registry = _policies()
        previous = registry['projects'].get(root, {})
        organization = organization or previous.get('organization')
        if not isinstance(organization, str) or not re.fullmatch(r'org_[A-Za-z0-9_-]{1,120}', organization):
            raise delta.DeltaError('organization_required', 'Choose an organization from delta org list.')
        if previous.get('enabled') and organization != previous.get('organization'):
            raise delta.DeltaError('capture_already_enabled', 'Disable capture before changing its upload organization.')
        since = previous.get('since') if previous.get('enabled') or not enabled else None
        registry['projects'][root] = {'enabled': enabled, 'organization': organization,
                                      'since': since or utc_now()}
        _save(_root() / 'projects.json', registry)
    return project_status(root)


def _state_directory(project, policy):
    # Re-enabling starts a fresh consent epoch; old pending text is not replayed.
    key = hashlib.sha256((project + '\0' + policy['since']).encode()).hexdigest()[:24]
    return _root() / 'captures' / key


def _session_files(sessions, since):
    if not sessions.exists():
        return []
    if sessions.is_symlink():
        raise delta.DeltaError('unsafe_session_path', 'Codex sessions must be a regular local directory.')
    selected = []
    count = 0
    for directory, dirs, files in os.walk(sessions, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            if not name.startswith('rollout-') or not name.endswith('.jsonl'):
                continue
            count += 1
            if count > 20000:
                raise delta.DeltaError('session_scan_limit', 'More than 20000 session files; narrow the Codex home used by this observer.')
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            if stat.st_mtime >= since:
                selected.append((path, [stat.st_mtime_ns, stat.st_size]))
    return selected


def run_once(retry_errors=False):
    from honey import delta_capture
    with store.session_lock(str(_root() / 'observer')):
        policies = {p: v for p, v in _policies()['projects'].items() if v['enabled']}
        previous = _read(_root() / 'observer.json', {'sessions': {}})
        prior = previous.get('sessions', {})
        if (not isinstance(prior, dict) or any(not isinstance(row, dict)
                or not isinstance(row.get('result', {}), dict) for row in prior.values())):
            raise delta.DeltaError('invalid_capture_state', 'Repair the observer session index.')
        # Keep durable delivery status even if Codex moves or deletes a rollout.
        rows = {key: row for key, row in prior.items() if row.get('project') in policies
                and row.get('since') == policies[row['project']]['since']}
        now = time.time()
        deadline = time.monotonic() + 60
        paused = bool(os.environ.get('HONEY_DISABLE'))
        if policies and not paused:
            files = _session_files(Path(paths.codex_dir()) / 'sessions', min(_since(v['since']) for v in policies.values()))
            for project, policy in policies.items():
                for file, signature in files:
                    if time.monotonic() >= deadline:
                        break
                    key = hashlib.sha256((project + '\0' + policy['since'] + '\0' + str(file)).encode()).hexdigest()
                    old = prior.get(key, {})
                    result = old.get('result', {})
                    retry = result.get('pending', 0) or result.get('backlog') or result.get('status') == 'error' or (
                        result.get('thread') and (result.get('cloud') or {}).get('state') != 'uploaded')
                    changed = old.get('signature') != signature
                    # Failures back off even while the source continues growing.
                    backoff = result.get('status') == 'error' and not retry_errors and (
                        result.get('retryable') is not True or now < old.get('retry_at', 0))
                    if not backoff and (changed or retry and (retry_errors or now >= old.get('retry_at', 0))):
                        result = delta_capture.capture_file(file, project, policy['since'], policy['organization'],
                                                            _state_directory(project, policy))
                        failures = old.get('failures', 0) + 1 if result.get('status') == 'error' else 0
                        delay = min(900, INTERVAL * 2 ** min(failures, 6))
                        requested_delay = result.get('retry_after_ms')
                        if type(requested_delay) is int and requested_delay >= 0:
                            delay = max(delay, requested_delay / 1000)
                        old = {'signature': signature, 'result': result, 'project': project, 'since': policy['since'],
                               'transcript': str(file),
                               'checked_at': utc_now(), 'failures': failures,
                               'retry_at': now + delay}
                    rows[key] = old
            for row in rows.values():
                if row.get('result', {}).get('pending') and not Path(row.get('transcript', '')).is_file():
                    row['result'] = dict(row['result'], status='error', error='transcript_unavailable', retryable=False)
        value = {'checked_at': utc_now(), 'pid': os.getpid(), 'paused': paused,
                 'enabled_projects': len(policies), 'sessions': rows}
        _save(_root() / 'observer.json', value)
    return status()


def _plist_path():
    return Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')


def service_status():
    result = {'supported': sys.platform == 'darwin', 'installed': _plist_path().is_file(), 'registered': False}
    if sys.platform == 'darwin' and result['installed']:
        try:
            completed = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{LABEL}'],
                                       capture_output=True, timeout=5)
            result['registered'] = completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            result['error'] = 'service_status_unavailable'
    return result


def status(project=None):
    observer = _read(_root() / 'observer.json', {})
    root = _project(project) if project else None
    sessions = []
    rows = observer.get('sessions', {})
    if not isinstance(rows, dict) or any(not isinstance(row, dict) or not isinstance(row.get('result', {}), dict) for row in rows.values()):
        raise delta.DeltaError('invalid_capture_state', 'Repair the observer session index.')
    for row in rows.values():
        if root and row.get('project') != root:
            continue
        result = row.get('result', {})
        if result.get('status') == 'ignored':
            continue
        sessions.append(dict(result, project=row.get('project'), checked_at=row.get('checked_at')))
    checked = observer.get('checked_at')
    try:
        fresh = bool(checked) and 0 <= time.time() - _since(checked) < 90
    except (ValueError, OverflowError):
        raise delta.DeltaError('invalid_capture_state', 'Repair the observer timestamp.') from None
    return {'projects': [project_status(root)] if root else [project_status(p) for p in _policies()['projects']],
            'service': service_status(), 'observer': {'checked_at': checked, 'recent': fresh,
            'paused': observer.get('paused', False)}, 'sessions': sessions}


def install_service():
    if sys.platform != 'darwin':
        raise delta.DeltaError('service_unsupported', 'Use honey delta capture run under your service manager on this platform.')
    launcher = Path.home() / '.local' / 'bin' / 'honey'
    if not launcher.is_file():
        raise delta.DeltaError('launcher_missing', 'Install Honey before installing the capture service.')
    _root().mkdir(parents=True, exist_ok=True, mode=0o700)
    log = _root() / 'service.log'
    if not log.exists():
        log.touch(mode=0o600)
    definition = {'Label': LABEL, 'ProgramArguments': [sys.executable, str(launcher), 'delta', 'capture', 'run', '--once'],
                  'RunAtLoad': True, 'StartInterval': INTERVAL, 'ProcessType': 'Background',
                  'StandardOutPath': str(log), 'StandardErrorPath': str(log),
                  'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
                                           'HONEY_HOME': str(Path(paths.home()).resolve()),
                                           'CODEX_HOME': str(Path(paths.codex_dir()).resolve())}}
    path = _plist_path()
    existing = path.read_bytes() if path.exists() else None
    content = plistlib.dumps(definition)
    if existing != content or not service_status()['registered']:
        if service_status()['registered']:
            completed = subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], capture_output=True, timeout=10)
            if completed.returncode:
                raise delta.DeltaError('service_stop_failed', 'The previous capture service could not be stopped.')
        delta.write_private(path, content.decode())
        completed = subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], capture_output=True, timeout=10)
        if completed.returncode:
            raise delta.DeltaError('service_start_failed', 'Capture is configured, but launchctl could not start its observer.')
    return service_status()


def uninstall_service():
    if sys.platform != 'darwin':
        raise delta.DeltaError('service_unsupported', 'Stop the observer in your service manager.')
    if service_status()['registered']:
        completed = subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], capture_output=True, timeout=10)
        if completed.returncode:
            raise delta.DeltaError('service_stop_failed', 'The capture service could not be stopped.')
    _plist_path().unlink(missing_ok=True)
    return service_status()


def main(argv):
    parser = argparse.ArgumentParser(prog='honey delta capture', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    enable = commands.add_parser('enable', help='Opt this workspace into private future conversation capture')
    enable.add_argument('--project')
    enable.add_argument('--organization', required=True)
    enable.add_argument('--no-service', action='store_true', help='Configure only; an external observer must be running')
    disable = commands.add_parser('disable', help='Stop Honey recording new messages for this workspace')
    disable.add_argument('--project')
    inspect = commands.add_parser('status', help='Show policy, observer and recorded session status')
    inspect.add_argument('--project')
    run = commands.add_parser('run', help='Observe opted-in Codex transcripts')
    run.add_argument('--once', action='store_true')
    run.add_argument('--retry', action='store_true', help='Explicitly retry paused errors, preserving original pending records')
    commands.add_parser('install-service', help='Install the current user macOS observer')
    commands.add_parser('uninstall-service', help='Remove the current user macOS observer')
    args = parser.parse_args(argv)
    if args.command == 'enable':
        result = configure(args.project, args.organization, True)
        result['service'] = service_status() if args.no_service else install_service()
    elif args.command == 'disable':
        result = configure(args.project, None, False)
    elif args.command == 'status':
        result = status(args.project)
    elif args.command == 'install-service':
        result = install_service()
    elif args.command == 'uninstall-service':
        result = uninstall_service()
    else:
        while True:
            run_once(retry_errors=args.retry)
            if args.once:
                return 0  # The observer persists diagnostics; do not append them forever to its service log.
            time.sleep(INTERVAL)
    print(json.dumps(result, ensure_ascii=False))
    return 0
