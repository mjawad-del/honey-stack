"""One bounded improvement attempt per day, driven by ordinary detached session jobs.

The caller owns store.lock. Generation creates evaluated child proposals; drafting
is never recorded as application. No proposal text is executed as a command.
"""
from __future__ import annotations

import json
import os
import re
import time

from honey import paths
from honey.rsi import autonomy, patch, review, store, text

INTERVAL_SECONDS = 24 * 3600


def _skill(proposal: dict):
    name = proposal.get('skill')
    if not name:
        pattern = (r'^Patch the ([a-z][a-z0-9-]*) skill \(' if proposal.get('type') == 'skill_patch'
                   else r'^([a-z][a-z0-9-]*) skill:')
        match = re.match(pattern, proposal.get('title', ''))
        name = match.group(1) if match else None
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]*', name):
        return None
    return name if os.path.isfile(os.path.join(paths.source_repo(), 'skills', name, 'SKILL.md')) else None


def run() -> dict:
    """Prepare at most one child; a preparation failure cannot stop application."""
    try:
        result = _run()
        if autonomy.enabled():
            state_file = paths.path('state', 'improvement-last.json')
            latest = store.read_json(state_file, {}) or {}
            latest.update(last_checked_at=text.now(), last_check_result=result.get('result', result))
            store.write_json(state_file, latest)
        return result
    except Exception as exc:
        state_file = paths.path('state', 'improvement-last.json')
        previous = store.read_json(state_file, {}) or {}
        out = dict(previous, ts=text.now(), result={'error': text.redact(str(exc), 400)})
        store.write_json(state_file, out)
        return out


def _run() -> dict:
    if not autonomy.enabled():
        return {'skipped': 'autonomy disabled'}
    from honey.rsi import publish
    publish.reconcile()
    state_file = paths.path('state', 'improvement-last.json')
    previous = store.read_json(state_file, {}) or {}
    pending = review.pending()
    pending_patches = [p for p in pending if p.get('type') == 'skill_patch' and
                       (not review.remote_block_reason(p) or
                        (p.get('application') or {}).get('state') == 'sync_pending')]
    if any(p.get('attempt_state') != 'blocked' or
           (p.get('application') or {}).get('state') == 'sync_pending' for p in pending_patches):
        return {'skipped': 'waiting for pending evaluated patch'}
    last_skip = (previous.get('result') or {}).get('skipped', '')
    was_budget_deferred = isinstance(last_skip, str) and last_skip.startswith(
        ('weekly_usd budget', 'monthly_usd budget'))
    if not was_budget_deferred and time.time() - previous.get('attempted_at', 0) < INTERVAL_SECONDS:
        return {'skipped': 'daily improvement cooldown'}
    choices = []
    verified_quotes = {}
    attempted = previous.get('targets', {})
    diagnosed_skills = {_skill(p) for p in review.all_proposals()
                        if p.get('type') in ('toolkit_fix', 'upstream_port') and
                        (review.remote_block_reason(p) or p.get('status') != 'applied' and
                         (p.get('child') or p.get('status') in ('skipped', 'never', 'closed')))}
    unavailable_skills = diagnosed_skills | {_skill(p) for p in pending_patches}
    diagnosed_skills.update(unavailable_skills)
    for p in pending:
        if review.remote_block_reason(p):
            continue
        if p.get('type') == 'toolkit_fix' and not p.get('repair') and not p.get('child'):
            skill = _skill(p)
            if skill and skill not in unavailable_skills:
                diagnosed_skills.add(skill)
                verified_quotes[skill] = patch.evidence_for(skill)
                if verified_quotes[skill]:
                    choices.append((p.get('prepared_at', 0), p['id'], p, skill))
                else:
                    p['preparation'] = {'blocked': 'skill diagnosis has no verified user evidence'}
                    store.write_json(paths.path('proposals', p['id'] + '.json'), p)
        elif p.get('type') == 'upstream_port' and not p.get('child'):
            skill = _skill(p)
            if skill and skill not in unavailable_skills and isinstance(p.get('upstream'), str) and p['upstream'].strip():
                diagnosed_skills.add(skill)
                choices.append((p.get('prepared_at', 0), p['id'], p, skill))
        elif p.get('type') == 'skill_new' and not p.get('child'):
            sessions = {e.get('sid') for e in p.get('evidence', [])
                        if e.get('sid') and e.get('origin') == 'user' and e.get('q')
                        and not e.get('quarantined') and not text.injection_hit(e['q'])}
            if len(sessions) >= 2:
                choices.append((p.get('prepared_at', 0), p['id'], p, None))
            else:
                p['preparation'] = {'blocked': 'new skill needs source-backed user quotes from two sessions'}
                store.write_json(paths.path('proposals', p['id'] + '.json'), p)
    for skill, status in patch.candidates():
        if skill not in diagnosed_skills and patch.evidence_for(skill):
            choices.append((attempted.get('skill:' + skill, 0), 'skill:' + skill, None, skill))
    if not choices:
        return {'skipped': 'no actionable skill evidence'}
    _, ident, proposal, skill = min(choices, key=lambda row: row[:2])
    blocked = patch._budget_block()
    if blocked:
        # No provider call has started. Preserve the previous real-attempt clock.
        out = dict(previous, ts=text.now(), id=ident, result={'skipped': blocked})
        store.write_json(state_file, out)
        if proposal:
            proposal['preparation'] = out['result']
            store.write_json(paths.path('proposals', ident + '.json'), proposal)
        return out
    now = time.time()
    attempted[ident] = now
    out = {'attempted_at': now, 'ts': text.now(), 'id': ident, 'targets': attempted}
    # Persist before the external call: crashes and failed calls also respect cooldown.
    store.write_json(state_file, out)
    if proposal:
        proposal['prepared_at'] = now
        store.write_json(paths.path('proposals', ident + '.json'), proposal)
    try:
        if proposal and proposal.get('type') == 'skill_new':
            result = patch.propose_new(proposal)
        elif proposal and proposal.get('type') == 'upstream_port':
            result = patch.propose(skill, 'upstream', proposal['upstream'])
        elif proposal:
            note = json.dumps({'verified_user_quotes': verified_quotes[skill]}, ensure_ascii=False)
            result = patch.propose(skill, 'diagnosis', note)
        else:
            result = patch.propose(skill, 'scorecard')
        if not isinstance(result, dict):
            raise ValueError('skill generator returned no structured result')
    except Exception as exc:
        result = {'error': text.redact(str(exc), 400)}
    if proposal:
        proposal['preparation'] = result
        if result.get('proposed'):
            proposal['child'] = result['proposed']
        store.write_json(paths.path('proposals', ident + '.json'), proposal)
    out['result'] = result
    store.write_json(state_file, out)
    return out
