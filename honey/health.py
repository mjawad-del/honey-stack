"""Bounded delivery heartbeats and read-only diagnostics for one actual session."""
from __future__ import annotations

import json
import math
import os
import re
import time

from honey import __version__, paths, tool_events

STALE_SECONDS = 15 * 60
MAX_TOOL_NAMES = 12
EVENTS = {'SessionStart', 'UserPromptSubmit', 'UserPromptExpansion', 'SubagentStart',
          'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Stop', 'SessionEnd',
          'PreCompact', 'PostCompact', 'Interrupt', 'Other'}
PLUGIN = 'honey@honey-stack'


def _count(value):
    return max(0, min(value, 1_000_000_000)) if type(value) is int else 0


def record(ctx):
    """Update only ctx.state; the dispatcher owns persistence and permission checks.

    Counters are delivered tool *events*, not unique tool calls or evidence that
    a guard ran. No commands, arguments, outputs, or user text enter a heartbeat.
    """
    if not getattr(ctx, 'sid', '') or getattr(ctx, 'sub', False):
        return
    current = ctx.state.get('health')
    current = current if isinstance(current, dict) else {}
    name = ctx.payload.get('hook_event_name')
    name = name if isinstance(name, str) and name in EVENTS else 'Other'
    stamp = time.time()
    events = current.get('events')
    events = {k: v for k, v in events.items() if k in EVENTS} if isinstance(events, dict) else {}
    events[name] = stamp
    current.update(last_seen=stamp, last_event=name, events=events, runtime_version=__version__,
                   received_events=_count(current.get('received_events')) + 1)
    if name in {'PreToolUse', 'PostToolUse', 'PostToolUseFailure'}:
        event = tool_events.normalize(ctx.payload)
        key = 'supported_events' if event['supported'] else 'unsupported_events'
        current[key] = _count(current.get(key)) + 1
        if not event['supported']:
            names = current.get('unsupported_tools')
            names = [n for n in names if isinstance(n, str)][:MAX_TOOL_NAMES] if isinstance(names, list) else []
            tool = re.sub(r'[^A-Za-z0-9_.:-]', '', event['tool'])[:80] or 'unknown'
            if tool not in names and len(names) < MAX_TOOL_NAMES:
                names.append(tool)
            current['unsupported_tools'] = names
    ctx.state['health'] = current
    ctx.dirty = True


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            value = json.loads(f.read(4 * 1024 * 1024))
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _bundle(path):
    if not isinstance(path, str) or not path:
        return False
    marker = _read_json(os.path.join(path, '.codex-plugin', 'plugin.json')) or _read_json(os.path.join(path, '.claude-plugin', 'plugin.json'))
    return (marker.get('name') == 'honey' and os.path.isfile(os.path.join(path, 'bin', 'honey'))
            and any(os.path.isfile(os.path.join(path, 'hooks', n)) for n in ('hooks.json', 'codex-hooks.json')))


def _installation(agent):
    import glob
    root = paths.codex_dir() if agent == 'codex' else paths.claude_dir()
    candidates = glob.glob(os.path.join(root, 'plugins', 'cache', 'honey-stack', 'honey', '*'))
    if agent == 'claude':
        installed = _read_json(os.path.join(root, 'plugins', 'installed_plugins.json')).get('plugins', {})
        if isinstance(installed, dict):
            entries = installed.get(PLUGIN, [])
            if isinstance(entries, list):
                candidates += [i.get('installPath') for i in entries if isinstance(i, dict)]
        marketplace = _read_json(os.path.join(root, 'plugins', 'known_marketplaces.json')).get('honey-stack', {})
        if isinstance(marketplace, dict) and isinstance(marketplace.get('source'), dict):
            candidates.append(marketplace['source'].get('path'))
        settings = _read_json(os.path.join(root, 'settings.json'))
        flags = settings.get('enabledPlugins', {})
        enabled = flags.get(PLUGIN, False) if isinstance(flags, dict) else None
        enabled = enabled if type(enabled) is bool else None
    else:
        try:
            with open(os.path.join(root, 'config.toml'), encoding='utf-8') as f:
                body = f.read(4 * 1024 * 1024)
            section = re.search(r'''^[ \t]*\[plugins\.(["'])honey@honey-stack\1\][ \t]*(?:\#[^\n]*)?\n((?:(?!^[ \t]*\[).)*)''', body, re.M | re.S)
            flag = re.search(r'^\s*enabled\s*=\s*(true|false)\s*(?:#.*)?$', section[2], re.M) if section else None
            enabled = flag[1] == 'true' if flag else (None if section else False)
        except FileNotFoundError:
            enabled = False
        except OSError:
            enabled = None
    locations = sorted(set(os.path.realpath(p) for p in candidates if _bundle(p)))
    return locations, enabled


def report(agent, session_id=None, now=None):
    """Inspect the named session, never substitute an unrelated recent session."""
    if agent not in {'claude', 'codex'}:
        raise ValueError('agent must be claude or codex')
    now = time.time() if now is None else now
    valid_id = isinstance(session_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', session_id)
    state = _read_json(paths.path('state', 'sessions', paths.session_key(agent, session_id) + '.json')) if valid_id else {}
    health = state.get('health')
    health = health if isinstance(health, dict) else {}
    locations, enabled = _installation(agent)
    stamp = health.get('last_seen')
    try:
        age = now - stamp if type(stamp) in (int, float) and math.isfinite(stamp) else None
    except OverflowError:
        age = None
    delivery = 'missing' if valid_id else ('missing_id' if not session_id else 'invalid_session')
    if age is not None:
        delivery = ('clock_skew' if age < -5 else 'ended' if health.get('last_event') == 'SessionEnd'
                    else 'stale' if age > STALE_SECONDS else 'observed')
    supported, unsupported = _count(health.get('supported_events')), _count(health.get('unsupported_events'))
    coverage = 'partial' if unsupported else 'supported' if supported else 'unobserved'
    names = health.get('unsupported_tools')
    names = [n for n in names if isinstance(n, str)][:MAX_TOOL_NAMES] if isinstance(names, list) else []
    paused = bool(state.get('off') or os.environ.get('HONEY_DISABLE') == '1' or os.environ.get('RSI_CHILD') == '1')
    version = health.get('runtime_version')
    events = health.get('events')
    events = events if isinstance(events, dict) else {}
    return {
        'agent': agent, 'session_id': session_id, 'installed': bool(locations), 'installed_paths': locations[:4],
        'enabled': enabled, 'receiving': delivery == 'observed', 'delivery': delivery,
        'last_seen': stamp, 'age_seconds': round(age, 1) if age is not None else None,
        'paused': paused, 'runtime_version': version, 'runtime_version_matches': version == __version__,
        'received_events': _count(health.get('received_events')),
        'events': sorted(k for k in events if k in EVENTS),
        'coverage': {'status': coverage, 'supported_events': supported, 'unsupported_events': unsupported,
                     'unsupported_tools': names},
        'enforcement': 'unverified',
        'healthy': bool(locations) and enabled is True and delivery == 'observed' and not paused
                   and version == __version__ and coverage == 'supported',
    }
