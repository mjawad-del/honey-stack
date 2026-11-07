"""Publish suggestion issues and applied skill PRs, excluding private evidence."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

from honey import paths
from honey.rsi import review, store, text


def command(args, *, env=None, input=None):
    result = subprocess.run(args, cwd=paths.source_repo(), env=env, input=input,
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(text.redact(result.stderr.strip(), 300) or 'Publication command failed')
    return result.stdout.strip()


def gh(*args):
    return command(['gh', *args])


def git(*args, **kwargs):
    return command(['git', *args], **kwargs)


def repository():
    remote = git('config', '--get', 'remote.origin.url')
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?', remote)
    if not match:
        raise ValueError('Publication requires a GitHub origin remote')
    push_urls = git('remote', 'get-url', '--push', '--all', 'origin').splitlines()
    if len(push_urls) != 1 or push_urls[0].removesuffix('.git') not in (
            'https://github.com/' + match.group(1), 'git@github.com:' + match.group(1)):
        raise ValueError('Publication push destination does not match origin')
    return match.group(1)


def config():
    return (store.read_json(paths.path('config.json'), {}) or {}).get('rsi', {}).get('publish', {})


def configure(action):
    cfg = store.read_json(paths.path('config.json'), {}) or {}
    settings = cfg.setdefault('rsi', {}).setdefault('publish', {})
    if action in ('on','automatic'):
        repo = repository()
        details = json.loads(gh('repo', 'view', repo, '--json', 'nameWithOwner,defaultBranchRef'))
        if details['nameWithOwner'].lower() != repo.lower():
            raise ValueError('GitHub repository does not match origin')
        settings.update(enabled=True, suggestions=True, repository=repo, base=details['defaultBranchRef']['name'])
        if action == 'automatic':
            settings['auto_merge'] = True
            cfg['rsi']['autonomous'] = True
            cfg['autoupdate'] = True
        else:
            settings.pop('auto_merge',None)
    elif action == 'off':
        settings['enabled'] = False
    if action != 'status':
        store.write_json(paths.path('config.json'), cfg)
    return settings


def save(proposal):
    store.write_json(paths.path('proposals', paths.safe_id(proposal['id']) + '.json'), proposal)


def public_text(value, limit):
    """Share bounded diagnoses, never raw quotes, identifiers or local paths."""
    value = text.redact(value if isinstance(value, str) else '', limit)
    value = re.sub(r'https?://\S+', '[link omitted]', value)
    value = re.sub(r'(?:~|/(?:Users|home|tmp|private))/(?:[^\s)]+)', '[path omitted]', value)
    return value.replace('@', '(at)')


def reconcile(settings=None):
    """Read exact published issues; closure retires a suggestion, never applies it.

    Caller owns the RSI lock. Only closures made here may be reopened here;
    explicit local apply/skip/never decisions remain authoritative.
    """
    settings = config() if settings is None else settings
    result = {'closed': [], 'reopened': [], 'blocked': []}
    if not settings.get('enabled'):
        return result
    proposals = [p for p in review.all_proposals()
                 if (p.get('github_issue') or {}).get('state') == 'published'
                 and (p.get('status') == 'pending' or
                      p.get('status') == 'applied' and p.get('decision_source') == 'automatic' or
                      p.get('status') == 'closed' and p.get('decision_source') == 'github_issue')]
    if not proposals:
        return result
    try:
        repo = settings['repository']
        if repository().lower() != repo.lower():
            raise ValueError('Publication origin changed; enable publication again to confirm it')
        pages = json.loads(gh('api', '--paginate', '--slurp',
                             f'repos/{repo}/issues?state=all&per_page=100'))
        issues = [issue for page in pages for issue in page if 'pull_request' not in issue]
        error = None
    except Exception as exc:
        issues, error = [], text.redact(str(exc), 300)
    for proposal in proposals:
        link = proposal['github_issue']
        try:
            if error:
                raise ValueError(error)
            url = link.get('url', '')
            if (link.get('repository', '').lower() != repo.lower() or
                    not re.fullmatch(r'https://github\.com/' + re.escape(repo) + r'/issues/[0-9]+', url, re.I)):
                raise ValueError('Suggestion issue identity does not match the configured repository')
            found = [i for i in issues if i.get('html_url', '').lower() == url.lower()]
            if len(found) != 1 or not found[0].get('title', '').startswith('RSI ' + proposal['id'] + ': '):
                raise ValueError('Exact published suggestion issue could not be verified')
            issue = found[0]
            state = issue.get('state', '').lower()
            if state not in ('open', 'closed'):
                raise ValueError('Suggestion issue has an unknown state')
            link.update(remote_state=state, state_reason=issue.get('state_reason'), checked_at=text.now())
            link.pop('reconciliation_error', None)
            if state == 'closed' and proposal['status'] == 'pending':
                link['closure'] = {'state_reason': issue.get('state_reason'), 'observed_at': text.now()}
                proposal.update(status='closed', decision_source='github_issue', decided=text.now())
                result['closed'].append(proposal['id'])
            elif state == 'open' and proposal['status'] == 'closed':
                proposal.update(status='pending', decided=None)
                proposal.pop('decision_source', None)
                result['reopened'].append(proposal['id'])
        except Exception as exc:
            link.update(reconciliation_error=text.redact(str(exc), 300), checked_at=text.now())
            result['blocked'].append({'id': proposal['id'], 'reason': link['reconciliation_error']})
        save(proposal)
    return result


def suggestions(settings):
    result = {'published': [], 'blocked': []}
    reconciled = reconcile(settings)
    if any(reconciled.values()):
        result['reconciled'] = reconciled
    if not settings.get('suggestions'):
        return result
    pending = [p for p in review.pending() if p.get('type') in ('toolkit_fix', 'skill_new', 'upstream_port')
               and (p.get('github_issue') or {}).get('state') != 'published']
    pending.sort(key=lambda p: (p.get('github_issue') or {}).get('updated', ''))
    issues = None
    for proposal in pending[:20]:
        try:
            repo = settings['repository']
            if repository().lower() != repo.lower():
                raise ValueError('Publication origin changed; enable publication again to confirm it')
            state = proposal.get('github_issue') or {}
            if state.get('repository') not in (None, repo):
                raise ValueError('Suggestion issue repository changed')
            if issues is None:
                # REST pagination avoids search-index lag after an ambiguous create response.
                pages = json.loads(gh('api', '--paginate', '--slurp',
                                     f'repos/{repo}/issues?state=all&per_page=100'))
                issues = [{'title': issue['title'], 'url': issue['html_url']}
                          for page in pages for issue in page if 'pull_request' not in issue]
            prefix = 'RSI ' + proposal['id'] + ': '
            found = [issue for issue in issues if issue['title'].startswith(prefix)]
            if len(found) > 1:
                raise ValueError('Multiple issues already exist for this suggestion')
            if found:
                url = found[0]['url']
            else:
                # New-skill titles are built from the user's own prompt keywords; never publish them.
                public_title = ('New skill suggestion for a repeated request' if proposal.get('type') == 'skill_new'
                                else public_text(proposal.get('title'), 160).replace('\n', ' '))
                title = prefix + public_title
                summary = public_text(proposal.get('rationale'), 600) or 'Investigate this potential workflow improvement.'
                fix = public_text(proposal.get('fix'), 600) or 'Confirm the diagnosis and prepare a tested change.'
                body = ('## RSI suggestion\n\n' + summary + '\n\n## Proposed next step\n\n' + fix +
                        '\n\nThis is an unimplemented suggestion, not a verified defect or an applied change. '
                        'Confirm the diagnosis and supporting evidence before implementation.\n\n'
                        'Raw session quotes, personal preferences and local evidence are not included.\n')
                with tempfile.TemporaryDirectory(prefix='honey-issue-') as directory:
                    file = os.path.join(directory, 'body.md')
                    store.write_text(file, body)
                    url = gh('issue', 'create', '--repo', repo, '--title', title, '--body-file', file)
                issues.append({'title': title, 'url': url})
            if not re.fullmatch(r'https://github\.com/' + re.escape(repo) + r'/issues/[0-9]+', url, re.I):
                raise ValueError('Suggestion issue URL does not match the configured repository')
            proposal['github_issue'] = {'state': 'published', 'repository': repo, 'url': url, 'updated': text.now()}
            save(proposal)
            result['published'].append({'id': proposal['id'], 'url': url})
        except Exception as exc:
            error = text.redact(str(exc), 300)
            proposal.setdefault('github_issue', {}).update(state='blocked', error=error, updated=text.now())
            save(proposal)
            result['blocked'].append({'id': proposal['id'], 'reason': error})
    if result['published']:
        refreshed = reconcile(settings)
        if any(refreshed.values()):
            for key, values in refreshed.items():
                result.setdefault('reconciled', {'closed': [], 'reopened': [], 'blocked': []})[key].extend(values)
    return result


def prepare(proposal, settings):
    """Build a public commit from the remote base and a single validated blob."""
    from honey.rsi import patch
    skill = proposal.get('skill', '')
    if not re.fullmatch(r'[a-z][a-z0-9-]*', skill):
        raise ValueError('Publication requires a named skill')
    if (proposal.get('application') or {}).get('state') != 'applied':
        raise ValueError('Publication requires successful local sync')
    patch._validate_eval(proposal)
    if not proposal.get('source_snapshot'):
        raise ValueError('Publication requires an evaluated runtime snapshot')
    patch._check_runtime_snapshot(proposal, set())
    if git('status', '--porcelain', '--', *patch.RUNTIME_SURFACE):
        raise ValueError('Publication requires a clean runtime; local changes stay private')
    sha = proposal.get('patch_sha', '')
    if not re.fullmatch(r'[0-9a-f]{40,64}', sha):
        raise ValueError('Publication requires an exact evaluated commit')
    parents = git('rev-list', '--parents', '-n', '1', sha).split()
    if len(parents) != 2:
        raise ValueError('Publication requires a single-parent evaluated patch')
    base = settings['base']
    git('check-ref-format', 'refs/heads/' + base)
    git('fetch', '--no-tags', 'origin', 'refs/heads/' + base)
    base_sha = git('rev-parse', 'FETCH_HEAD')
    if git('diff', '--name-only', base_sha, parents[1], '--', *patch.RUNTIME_SURFACE):
        raise ValueError('Remote runtime differs from evaluation baseline; re-evaluate first')
    target = f'skills/{skill}/SKILL.md'
    if git('diff', '--name-only', parents[1], sha, '--', *patch.RUNTIME_SURFACE) != target:
        raise ValueError('Evaluated patch changed unexpected runtime files')
    entry = git('ls-tree', sha, '--', target).split()
    if len(entry) != 4 or entry[:2] != ['100644', 'blob'] or entry[3] != target:
        raise ValueError('Published skill must be a regular file')
    # An isolated index cannot stage user files. No private commit becomes an ancestor.
    with tempfile.TemporaryDirectory(prefix='honey-publish-') as directory:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(directory, 'index'))
        git('read-tree', base_sha, env=env)
        git('update-index', '--add', '--cacheinfo', '100644', entry[2], target, env=env)
        tree = git('write-tree', env=env)
        public_sha = git('commit-tree', tree, '-p', base_sha,
                         input=f'fix(skills): improve {skill} workflow\n')
    return {'state': 'prepared', 'sha': public_sha, 'source_sha': sha, 'base_sha': base_sha,
            'branch': 'rsi/publish-' + paths.safe_id(proposal['id']),
            'repository': settings['repository'], 'base': base, 'updated': text.now()}


def publish(proposal, settings):
    remote_block = review.remote_block_reason(proposal)
    if remote_block:
        raise ValueError(remote_block)
    if repository().lower() != settings['repository'].lower():
        raise ValueError('Publication origin changed; enable publication again to confirm it')
    state = proposal.get('publication') or {}
    if not state.get('sha'):
        state = prepare(proposal, settings)
        proposal['publication'] = state
        save(proposal)  # Persist the exact commit before any network write.
    if (state['source_sha'] != proposal.get('patch_sha') or
            any(state[key] != settings[key] for key in ('repository', 'base'))):
        raise ValueError('Publication target or evaluated commit changed')
    branch, sha, repo = state['branch'], state['sha'], state['repository']
    args = ('pr', 'list', '--repo', repo, '--head', branch, '--base', state['base'],
            '--state', 'all', '--json', 'url,headRefOid,state')
    prs = json.loads(gh(*args))
    if prs and (len(prs) != 1 or prs[0]['headRefOid'] != sha or prs[0]['state'] not in ('OPEN', 'MERGED')):
        raise ValueError('Publication PR is closed, ambiguous or has a different head')
    if prs and prs[0]['state'] == 'MERGED':
        state.update(state='published', url=prs[0]['url'], updated=text.now())
        state.pop('error', None)
        save(proposal)
        return {'id': proposal['id'], 'url': state['url'], 'sha': sha}
    remote = git('ls-remote', '--heads', 'origin', 'refs/heads/' + branch).split()
    if remote and remote[0] != sha:
        raise ValueError('Publication branch changed remotely; refusing to overwrite it')
    git('push', 'origin', sha + ':refs/heads/' + branch)
    remote = git('ls-remote', '--heads', 'origin', 'refs/heads/' + branch).split()
    if not remote or remote[0] != sha:
        raise ValueError('Published head could not be verified')
    if not prs:
        # Only fixed metadata is sent; rationale, evidence and generated evals stay local.
        with tempfile.TemporaryDirectory(prefix='honey-pr-') as directory:
            body = os.path.join(directory, 'body.md')
            store.write_text(body, 'Updates one skill after local regression evaluation and successful plugin sync.\n\n'
                             'Only the skill file is published. Private session evidence and generated evaluation '
                             'prompts remain local. Local evaluation does not replace repository CI or review.\n')
            gh('pr', 'create', '--repo', repo, '--base', state['base'], '--head', branch,
               '--title', 'fix(skills): improve ' + proposal['skill'] + ' workflow', '--body-file', body)
        prs = json.loads(gh(*args))
    if len(prs) != 1 or prs[0]['headRefOid'] != sha or prs[0]['state'] not in ('OPEN', 'MERGED'):
        raise ValueError('Publication PR is closed, ambiguous or has a different head')
    state.update(state='published', url=prs[0]['url'], updated=text.now())
    state.pop('error', None)
    save(proposal)
    return {'id': proposal['id'], 'url': state['url'], 'sha': sha}


def run(include_patches=True, suggestion_result=None):
    """Caller owns the RSI lock; publication failure never undoes local application."""
    settings = config()
    if not settings.get('enabled'):
        return {'skipped': 'publication disabled'}
    result = {'published': [], 'blocked': [],
              'suggestions': suggestions(settings) if suggestion_result is None else suggestion_result}
    pending = [p for p in review.all_proposals() if p.get('type') == 'skill_patch'
               and p.get('status') == 'applied' and (p.get('publication') or {}).get('state') != 'published']
    pending.sort(key=lambda p: (p.get('publication') or {}).get('updated', ''))
    for proposal in pending[:20] if include_patches else []:
        try:
            result['published'].append(publish(proposal, settings))
        except Exception as exc:
            error = text.redact(str(exc), 300)
            proposal.setdefault('publication', {}).update(state='blocked', error=error, updated=text.now())
            save(proposal)
            result['blocked'].append({'id': proposal['id'], 'reason': error})
    if include_patches and settings.get('auto_merge'):
        from honey.rsi import delivery
        result['delivery'] = delivery.run(settings)
    store.write_json(paths.path('state', 'publication-last.json'), dict(result, ts=text.now()))
    return result
