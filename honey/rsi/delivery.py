"""Opt-in merge/release/install completion for evaluated RSI skill patches."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from honey import install, paths
from honey.rsi import patch, publish, review, text

CHECKS = {f'test ({osname}, {version})' for osname in ('ubuntu-latest', 'macos-latest')
          for version in ('3.9', '3.12')}


def merge(proposal, settings):
    patch._validate_eval(proposal)
    publication = proposal['publication']
    if publication.get('source_sha') != proposal['patch_sha'] or publication.get('repository') != settings['repository']:
        raise ValueError('Publication no longer matches the evaluated patch or repository')
    if not re.fullmatch(r'https://github\.com/'+re.escape(settings['repository'])+r'/pull/\d+',publication['url']):
        raise ValueError('PR URL does not match the configured repository')
    fields = 'state,headRefOid,baseRefOid,isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,mergeCommit'
    args = ('pr','view',publication['url'],'--repo',settings['repository'],'--json',fields)
    info = json.loads(publish.gh(*args))
    if info['headRefOid'] != publication['sha']:
        raise ValueError('PR head changed after evaluation')
    if info['state'] == 'MERGED':
        return {'state':'merged','sha':info['mergeCommit']['oid']}
    if info['state'] != 'OPEN' or info['isDraft']:
        raise ValueError('PR is closed or draft')
    if info['baseRefOid'] != publication['base_sha']:
        raise ValueError('PR base changed; re-evaluation is required')
    checks = info['statusCheckRollup']
    for name in CHECKS:
        matches = [c for c in checks if c['name']==name]
        if not matches or any(c.get('status')!='COMPLETED' for c in matches):
            return {'state':'waiting_checks'}
        if any(c.get('conclusion')!='SUCCESS' for c in matches):
            raise ValueError('Required CI failed: '+name)
    if any(c.get('conclusion') in ('FAILURE','CANCELLED','TIMED_OUT','ACTION_REQUIRED') for c in checks):
        raise ValueError('An additional check failed')
    if any(c.get('status')!='COMPLETED' for c in checks):
        return {'state':'waiting_checks'}
    if info['reviewDecision'] == 'CHANGES_REQUESTED':
        raise ValueError('Review requested changes')
    # An empty decision means the repository requires no review; unattended merges still need an approval.
    if info.get('reviewDecision') != 'APPROVED':
        return {'state':'waiting_review'}
    if info['mergeable'] != 'MERGEABLE' or info['mergeStateStatus'] != 'CLEAN':
        return {'state':'waiting_mergeability'}
    # A normal fast-forward-only push gives the server an atomic base guard.
    # Both parents descend from exactly the tested base; newer main commits
    # therefore cannot be overwritten or silently included in this merge.
    base, head = publication['base_sha'], publication['sha']
    publish.git('fetch','origin',settings['base'],head)
    if publish.git('rev-list','--parents','-n','1',head).split() != [head,base]:
        raise ValueError('Published patch is not based directly on the evaluated base')
    tree = publish.git('rev-parse',head+'^{tree}')
    merged = publish.git('commit-tree',tree,'-p',base,'-p',head,
                         input='Merge evaluated RSI patch '+proposal['id']+'\n')
    publish.git('push','origin',merged+':refs/heads/'+settings['base'])
    # Successful push proves this exact tested tree landed. GitHub can take a
    # moment to reflect the corresponding PR's merged state.
    return {'state':'merged','sha':merged}


def verify_checkout(released):
    if publish.git('rev-parse','HEAD')!=released or publish.git('status','--porcelain'):
        raise ValueError('Source changed during installation; retry after preserving local work')


def verify_installed(released,tag):
    cache=Path(paths.codex_dir(),'plugins','cache','honey-stack','honey',tag[1:])
    names=publish.git('ls-tree','-r','--name-only',released,'--',*patch.RUNTIME_SURFACE).splitlines()
    if not names:
        raise ValueError('Released runtime contains no files to verify')
    for name in names:
        expected=subprocess.check_output(['git','-C',paths.source_repo(),'show',released+':'+name])
        target=cache/name
        if not target.is_file() or target.is_symlink() or target.read_bytes()!=expected:
            raise ValueError('Installed runtime does not match the release: '+name)


def install_release(proposal, settings):
    """Keep private evaluation history in a backup branch, never reset user work."""
    delivery = proposal['delivery']
    release = json.loads(publish.gh('release','view','--repo',settings['repository'],'--json','tagName,isDraft,isPrerelease'))
    tag = release['tagName']
    if release['isDraft'] or release['isPrerelease'] or not re.fullmatch(r'v\d+\.\d+\.\d+',tag):
        return {'state':'waiting_release'}
    publish.git('fetch','--tags','origin',settings['base'])
    released = publish.git('rev-parse',tag+'^{commit}')
    if publish.git('merge-base',delivery['sha'],released) != delivery['sha']:
        return {'state':'waiting_release'}
    if publish.git('branch','--show-current') != settings['base'] or publish.git('status','--porcelain'):
        raise ValueError('Local changes or branch selection block installation; work preserved')
    head = publish.git('rev-parse','HEAD')
    if publish.git('merge-base',head,released) == head:
        publish.git('merge','--ff-only',released)
    elif head != released:
        if head != proposal['patch_sha'] or (proposal.get('application') or {}).get('merged_sha') != head:
            raise ValueError('Local history changed; automatic reconciliation refused')
        backup = 'rsi/applied-backup-'+paths.safe_id(proposal['id'])
        if publish.git('branch','--list',backup):
            raise ValueError('Backup branch already exists; preserve it and inspect local history')
        publish.git('branch','-m',settings['base'],backup)
        try:
            publish.git('switch','-c',settings['base'],released)
        except Exception:
            publish.git('branch','-m',backup,settings['base'])
            raise
    verify_checkout(released)
    if install.sync(install.Env()) != 0:
        raise ValueError('Released source is ready but plugin installation failed; retry required')
    verify_checkout(released)
    verify_installed(released,tag)
    verify_checkout(released)
    return {'state':'installed','tag':tag,'sha':released}


def run(settings):
    result = {'merged':[],'waiting':[],'blocked':[],'installed':[]}
    if not settings.get('auto_merge'):
        return result
    pending = [p for p in review.all_proposals() if p.get('type')=='skill_patch' and p.get('status')=='applied'
               and (p.get('publication') or {}).get('state')=='published'
               and (p.get('delivery') or {}).get('installation',{}).get('state')!='installed']
    pending.sort(key=lambda p:(p.get('delivery') or {}).get('updated',''))
    for proposal in pending[:20]:
        try:
            remote_block = review.remote_block_reason(proposal)
            if remote_block:
                raise ValueError(remote_block)
            if publish.repository().lower()!=settings['repository'].lower():
                raise ValueError('Publication repository changed')
            current=proposal.get('delivery') or {}
            if current.get('state')!='merged':
                current=merge(proposal,settings)
                proposal['delivery']=current
                if current['state']=='merged':
                    result['merged'].append(proposal['id'])
            if current['state']=='merged':
                current['installation']=install_release(proposal,settings)
                key='installed' if current['installation']['state']=='installed' else 'waiting'
            else:
                key='waiting'
            current['updated']=text.now()
            current.pop('error',None)
            result[key].append({'id':proposal['id'],**current})
        except Exception as exc:
            error=text.redact(str(exc),300)
            proposal.setdefault('delivery',{}).update(error=error,updated=text.now())
            result['blocked'].append({'id':proposal['id'],'reason':error})
        publish.save(proposal)
    return result
