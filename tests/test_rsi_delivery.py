"""Automatic delivery must wait for verified CI and preserve local work."""
import json
import os
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from helpers import REPO
from honey import paths
from honey.rsi import publish, store


class AutoDelivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, HONEY_HOME=self.tmp.name)
        self.env.start(); self.addCleanup(self.env.stop)
        self.p = {'id':'p-test','type':'skill_patch','status':'applied','skill':'debug',
                  'patch_sha':'a'*40, 'publication':{'state':'published','sha':'b'*40,
                  'source_sha':'a'*40,'repository':'example/honey',
                  'base_sha':'c'*40,'url':'https://github.com/example/honey/pull/1'}}
        self.settings = {'enabled':True,'auto_merge':True,'repository':'example/honey','base':'main'}
        self.info = {'state':'OPEN','headRefOid':'b'*40,'baseRefOid':'c'*40,'isDraft':False,
            'mergeable':'MERGEABLE','mergeStateStatus':'CLEAN','reviewDecision':'APPROVED',
            'statusCheckRollup':[{'name':f'test ({osname}, {version})','status':'COMPLETED','conclusion':'SUCCESS'}
                for osname in ('ubuntu-latest','macos-latest') for version in ('3.9','3.12')]}

    def test_merge_waits_for_all_ci_then_uses_exact_head(self):
        from honey.rsi import delivery
        self.info['statusCheckRollup'][0]['status']='IN_PROGRESS'
        with patch.object(publish,'gh',return_value=json.dumps(self.info)) as gh, \
             patch('honey.rsi.patch._validate_eval'):
            result=delivery.merge(self.p,self.settings)
            self.assertEqual(result['state'],'waiting_checks')
            self.assertFalse(any(c.args[:2]==('pr','merge') for c in gh.call_args_list))

    def test_successful_merge_pushes_only_tested_tree(self):
        from honey.rsi import delivery
        def git(*args, **kwargs):
            if args[0]=='rev-list': return ' '.join(['b'*40,'c'*40])
            if args[0]=='rev-parse': return 'e'*40
            if args[0]=='commit-tree':
                self.assertEqual(args[1:],('e'*40,'-p','c'*40,'-p','b'*40))
                return 'd'*40
            return ''
        with patch.object(publish,'gh',return_value=json.dumps(self.info)),patch('honey.rsi.patch._validate_eval'),patch.object(publish,'git',side_effect=git) as commands:
            self.assertEqual(delivery.merge(self.p,self.settings),{'state':'merged','sha':'d'*40})
            self.assertEqual(commands.call_args.args,('push','origin','d'*40+':refs/heads/main'))

    def test_changed_head_base_failed_ci_and_review_block_merge(self):
        from honey.rsi import delivery
        cases=[{'headRefOid':'e'*40},{'baseRefOid':'e'*40},{'reviewDecision':'CHANGES_REQUESTED'},
               {'state':'CLOSED'},{'isDraft':True}]
        for update in cases:
            with self.subTest(update=update),patch.object(publish,'gh',return_value=json.dumps(dict(self.info,**update))) as gh,patch('honey.rsi.patch._validate_eval'):
                with self.assertRaises(ValueError): delivery.merge(self.p,self.settings)
                self.assertEqual(gh.call_count,1)
        self.info['statusCheckRollup'][0]['conclusion']='FAILURE'
        with patch.object(publish,'gh',return_value=json.dumps(self.info)) as gh,patch('honey.rsi.patch._validate_eval'):
            with self.assertRaises(ValueError): delivery.merge(self.p,self.settings)
            self.assertEqual(gh.call_count,1)

    def test_missing_evaluation_cannot_merge(self):
        from honey.rsi import delivery
        with patch.object(publish,'gh') as gh:
            with self.assertRaises(ValueError): delivery.merge(self.p,self.settings)
            gh.assert_not_called()

    def test_review_and_missing_checks_wait_without_merging(self):
        from honey.rsi import delivery
        for update in ({'reviewDecision':'REVIEW_REQUIRED'},{'reviewDecision':''},{'reviewDecision':None},
                       {'statusCheckRollup':[]}):
            with patch.object(publish,'gh',return_value=json.dumps(dict(self.info,**update))) as gh,patch('honey.rsi.patch._validate_eval'):
                self.assertTrue(delivery.merge(self.p,self.settings)['state'].startswith('waiting'))
                self.assertEqual(gh.call_count,1)

    def test_existing_merged_pr_is_recovered(self):
        from honey.rsi import delivery
        self.info.update(state='MERGED',mergeCommit={'oid':'d'*40})
        with patch.object(publish,'gh',return_value=json.dumps(self.info)) as gh,patch('honey.rsi.patch._validate_eval'):
            self.assertEqual(delivery.merge(self.p,self.settings)['state'],'merged')
            self.assertEqual(gh.call_count,1)

    def test_delivery_retries_install_without_remerging(self):
        from honey.rsi import delivery
        store.write_json(paths.path('proposals','p-test.json'),self.p)
        with patch.object(publish,'repository',return_value='example/honey'), \
             patch.object(delivery,'merge',return_value={'state':'merged','sha':'d'*40}) as merge, \
             patch.object(delivery,'install_release',side_effect=[{'state':'waiting_release'},{'state':'installed','tag':'v1.0.0'}]):
            self.assertEqual(len(delivery.run(self.settings)['waiting']),1)
            self.assertEqual(len(delivery.run(self.settings)['installed']),1)
            merge.assert_called_once()
        saved=store.read_json(paths.path('proposals','p-test.json'))
        self.assertEqual(saved['status'],'applied')
        self.assertEqual(saved['delivery']['installation']['state'],'installed')

    def test_disabled_delivery_does_not_call_github(self):
        from honey.rsi import delivery
        store.write_json(paths.path('proposals','p-test.json'),self.p)
        with patch.object(publish,'gh') as gh:
            self.assertEqual(delivery.run({})['merged'],[])
            gh.assert_not_called()

    def test_automatic_opt_in_and_review_only_switch(self):
        with patch.object(publish,'repository',return_value='example/honey'),patch.object(publish,'gh',return_value=json.dumps({'nameWithOwner':'example/honey','defaultBranchRef':{'name':'main'}})):
            self.assertTrue(publish.configure('automatic')['auto_merge'])
            config=store.read_json(paths.path('config.json'))
            self.assertTrue(config['rsi']['autonomous'])
            self.assertTrue(config['autoupdate'])
            self.assertNotIn('auto_merge',publish.configure('on'))
            self.assertFalse(publish.configure('off')['enabled'])


class Installation(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name,'source');self.root.mkdir()
        self.remote=Path(self.tmp.name,'remote.git')
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,HONEY_HOME=str(Path(self.tmp.name,'state')),CODEX_HOME=str(Path(self.tmp.name,'codex'))).start()
        patch.object(paths,'source_repo',return_value=str(self.root)).start()
        self.git('init','-q','-b','main');self.git('config','user.name','Test');self.git('config','user.email','test@example.invalid')
        (self.root/'honey').mkdir(); (self.root/'honey/module.py').write_text('before')
        self.git('add','.');self.git('commit','-qm','base');base=self.git('rev-parse','HEAD')
        (self.root/'honey/module.py').write_text('after');(self.root/'private-evaluation').write_text('private')
        self.git('add','.');self.git('commit','-qm','local applied patch');self.private=self.git('rev-parse','HEAD')
        subprocess.run(['git','init','--bare','-q',str(self.remote)],check=True)
        self.git('remote','add','origin',str(self.remote));self.git('push','-q','origin',base+':refs/heads/main')
        public=Path(self.tmp.name,'public')
        subprocess.run(['git','clone','-q','-b','main',str(self.remote),str(public)],check=True)
        def git(*args):
            return subprocess.check_output(['git','-C',str(public),*args],text=True,stderr=subprocess.DEVNULL).strip()
        git('config','user.name','Test');git('config','user.email','test@example.invalid')
        (public/'honey/module.py').write_text('after');git('commit','-qam','public patch');self.merged=git('rev-parse','HEAD')
        (public/'version').write_text('1.1.0');git('add','.');git('commit','-qm','release');git('tag','v1.1.0')
        self.released=git('rev-parse','HEAD');git('push','origin','main','--tags')
        self.git('fetch','origin','main','--tags')
        self.p={'id':'p-test','patch_sha':self.private,'application':{'merged_sha':self.private},'delivery':{'sha':self.merged}}
        self.settings={'repository':'example/honey','base':'main'}
        patch.object(publish,'gh',return_value=json.dumps({'tagName':'v1.1.0','isDraft':False,'isPrerelease':False})).start()
        self.sync=patch('honey.install.sync',side_effect=self.install).start()

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.root),*args],text=True,stderr=subprocess.DEVNULL).strip()

    def install(self, env):
        cache=Path(paths.codex_dir(),'plugins/cache/honey-stack/honey/1.1.0/honey')
        cache.mkdir(parents=True,exist_ok=True)
        (cache/'module.py').write_bytes((self.root/'honey/module.py').read_bytes())
        return 0

    def test_private_history_is_preserved_before_installing_release(self):
        from honey.rsi import delivery
        self.assertEqual(delivery.install_release(self.p,self.settings)['state'],'installed')
        self.assertEqual(self.git('rev-parse','HEAD'),self.released)
        self.assertEqual(self.git('branch','--show-current'),'main')
        self.assertEqual(self.git('rev-parse','rsi/applied-backup-p-test'),self.private)
        self.assertEqual(self.git('show','rsi/applied-backup-p-test:private-evaluation'),'private')
        self.assertFalse((self.root/'private-evaluation').exists())
        self.sync.assert_called_once()

    def test_local_work_is_preserved_and_install_waits(self):
        from honey.rsi import delivery
        (self.root/'user-work').write_text('keep')
        with self.assertRaises(ValueError):delivery.install_release(self.p,self.settings)
        self.assertEqual(self.git('rev-parse','HEAD'),self.private)
        self.assertEqual((self.root/'user-work').read_text(),'keep')
        self.sync.assert_not_called()

    def test_later_local_commit_cannot_be_replaced(self):
        from honey.rsi import delivery
        (self.root/'user-work').write_text('keep');self.git('add','.');self.git('commit','-qm','user commit')
        head=self.git('rev-parse','HEAD')
        with self.assertRaises(ValueError):delivery.install_release(self.p,self.settings)
        self.assertEqual(self.git('rev-parse','HEAD'),head)
        self.sync.assert_not_called()

    def test_edit_during_checkout_cannot_be_installed(self):
        from honey.rsi import delivery
        original=publish.git
        def edit_after_switch(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[:2]==('switch','-c'):
                (self.root/'honey/module.py').write_text('concurrent edit')
            return result
        with patch.object(publish,'git',side_effect=edit_after_switch):
            with self.assertRaises(ValueError):delivery.install_release(self.p,self.settings)
        self.sync.assert_not_called()

    def test_install_failure_is_retryable_after_history_reconciliation(self):
        from honey.rsi import delivery
        self.sync.side_effect=None
        self.sync.return_value=1
        with self.assertRaises(ValueError):delivery.install_release(self.p,self.settings)
        self.assertEqual(self.git('rev-parse','HEAD'),self.released)
        self.sync.side_effect=self.install
        self.assertEqual(delivery.install_release(self.p,self.settings)['state'],'installed')
        self.assertEqual(self.git('rev-parse','rsi/applied-backup-p-test'),self.private)

    def test_edit_during_sync_is_not_reported_as_installed(self):
        from honey.rsi import delivery
        def sync(env):
            self.install(env)
            (self.root/'honey/module.py').write_text('concurrent edit')
            return 0
        self.sync.side_effect=sync
        with self.assertRaises(ValueError):delivery.install_release(self.p,self.settings)

    def test_wrong_installed_bytes_are_rejected(self):
        from honey.rsi import delivery
        def sync(env):
            self.install(env)
            Path(paths.codex_dir(),'plugins/cache/honey-stack/honey/1.1.0/honey/module.py').write_text('wrong')
            return 0
        self.sync.side_effect=sync
        with self.assertRaises(ValueError):delivery.install_release(self.p,self.settings)

    def test_main_race_cannot_overwrite_new_remote_commit(self):
        from honey.rsi import delivery
        base=self.git('rev-parse',self.merged+'^')
        proposal={'id':'race','patch_sha':self.private,'publication':{
            'source_sha':self.private,'sha':self.merged,'base_sha':base,
            'repository':'example/honey','url':'https://github.com/example/honey/pull/1'}}
        info={'state':'OPEN','headRefOid':self.merged,'baseRefOid':base,'isDraft':False,
            'mergeable':'MERGEABLE','mergeStateStatus':'CLEAN','reviewDecision':'APPROVED',
            'statusCheckRollup':[{'name':n,'status':'COMPLETED','conclusion':'SUCCESS'} for n in delivery.CHECKS]}
        # GitHub read was at base; remote has since advanced to the release.
        with patch.object(publish,'gh',return_value=json.dumps(info)),patch('honey.rsi.patch._validate_eval'):
            with self.assertRaises(RuntimeError):delivery.merge(proposal,self.settings)
        remote=self.git('ls-remote','origin','refs/heads/main').split()[0]
        self.assertEqual(remote,self.released)

    def test_merge_lands_exact_evaluated_tree(self):
        from honey.rsi import delivery
        # Use a fresh base branch, keeping the advanced main as race coverage.
        base=self.git('rev-parse',self.merged+'^')
        self.git('push','origin',base+':refs/heads/test-base')
        proposal={'id':'success','patch_sha':self.private,'publication':{
            'source_sha':self.private,'sha':self.merged,'base_sha':base,
            'repository':'example/honey','url':'https://github.com/example/honey/pull/1'}}
        info={'state':'OPEN','headRefOid':self.merged,'baseRefOid':base,'isDraft':False,
            'mergeable':'MERGEABLE','mergeStateStatus':'CLEAN','reviewDecision':'APPROVED',
            'statusCheckRollup':[{'name':n,'status':'COMPLETED','conclusion':'SUCCESS'} for n in delivery.CHECKS]}
        with patch.object(publish,'gh',return_value=json.dumps(info)),patch('honey.rsi.patch._validate_eval'):
            result=delivery.merge(proposal,dict(self.settings,base='test-base'))
        self.assertEqual(self.git('ls-remote','origin','refs/heads/test-base').split()[0],result['sha'])
        self.assertEqual(self.git('rev-parse',result['sha']+'^{tree}'),self.git('rev-parse',self.merged+'^{tree}'))


if __name__=='__main__':
    unittest.main()
