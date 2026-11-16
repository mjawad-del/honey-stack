"""Read-only global bootstrap and a small, reversible instruction-file entrypoint."""
from __future__ import annotations

import argparse
import os
import re
import shlex

from honey import health, modes, paths
from honey.rsi import curate, store

START = '<!-- honey:global:start -->'
END = '<!-- honey:global:end -->'


def block(launcher: str, agent: str) -> str:
    command = 'python3 ' + shlex.quote(paths.shell_path(launcher)) + ' bootstrap --agent ' + agent
    return (START + '\n## Honey global activation\n'
            'At the first user turn of each session, if no native `honey-stack active` startup context was supplied, '
            'run `' + command + '` and follow its returned workflow rules. Otherwise follow the supplied native Honey context. '
            'This command only reads configuration; do not invoke hook handlers or manufacture lifecycle events.\n'
            'Use the returned `🍯 HONEY` indicator at the start of user-facing replies. If it says '
            '`hooks unverified`, retain that label; instruction loading is not proof of hook delivery. '
            'If the bootstrap cannot run, report Honey as unavailable instead of claiming it is active. '
            'Explicit user instructions and `honey off` take precedence.\n' + END)


def replace_block(body: str, replacement: str) -> tuple:
    if body.count(START) != body.count(END) or body.count(START) > 1:
        raise ValueError('Honey global activation markers are incomplete or duplicated')
    match = re.search(re.escape(START) + r'.*?' + re.escape(END), body, re.S)
    if match:
        return body[:match.start()] + replacement + body[match.end():], match.group()
    separator = '' if not body or body.endswith('\n\n') else '\n' if body.endswith('\n') else '\n\n'
    return body + separator + replacement + '\n', None


def context(agent: str, session=None) -> str:
    if os.environ.get('HONEY_DISABLE') == '1' or os.environ.get('RSI_CHILD') == '1':
        return 'Honey is disabled in this isolated subprocess.'
    session = session or os.environ.get('CODEX_THREAD_ID' if agent == 'codex' else 'CLAUDE_SESSION_ID')
    saved = store.read_json(paths.path('state', 'sessions', paths.session_key(agent, session) + '.json'), {}) if session else {}
    if not isinstance(saved, dict):
        saved = {}
    state = {'mode': modes.selected(saved, modes.defaults()['mode'])}
    if saved.get('off'):
        return 'Honey is paused for this session. Honor honey off until the user resumes it.'
    report = health.report(agent, session)
    delivered = (report['receiving'] and report['installed'] and report['enabled'] is True
                 and not report['paused'] and report['runtime_version_matches'])
    body = modes.render(agent, state)
    if not delivered:
        indicator = modes.indicator(state)
        body = body.replace('`' + indicator + '`', '`' + indicator + ' · hooks unverified`', 1)
        body += '\nGlobal workflow instructions are loaded. Native hook delivery is unverified; do not claim otherwise.'
    if not saved.get('rsi_off'):
        brief, _ = curate.brief(agent, saved.get('project') or paths.project_slug(os.getcwd()))
        if brief:
            body += '\n\n' + brief
    return body


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog='honey bootstrap', description='Read global workflow context without firing hooks.')
    parser.add_argument('--agent', choices=('codex', 'claude'), required=True)
    parser.add_argument('--session')
    args = parser.parse_args(argv)
    print(context(args.agent, args.session))
    return 0
