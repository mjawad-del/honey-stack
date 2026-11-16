"""Installed RSI resolves and synchronizes the real source without invoking agent CLIs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch as mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from honey import install, paths
from honey.rsi import patch, store


class SourceCheckout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = mock.dict(os.environ, HONEY_HOME=str(self.root / 'honey'))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'Test')
        (self.repo / 'honey').mkdir()
        (self.repo / 'honey' / '__init__.py').write_text('', encoding="utf-8")
        (self.repo / '.codex-plugin').mkdir()
        (self.repo / '.codex-plugin' / 'plugin.json').write_text('{"name":"honey"}', encoding="utf-8")
        self.git('add', '.'); self.git('commit', '-qm', 'source')
        self.cache = self.root / 'plugins' / 'cache' / 'honey-stack' / 'honey' / '0.9.1'
        self.cache.mkdir(parents=True)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True, text=True, encoding='utf-8').stdout.strip()

    def test_source_and_installed_runtime(self):
        with mock.object(paths, 'REPO', str(self.repo)):
            self.assertEqual(paths.source_repo(), str(self.repo))
        store.write_json(paths.path('synced.json'), {'source_repo': str(self.repo)})
        with mock.object(paths, 'REPO', str(self.cache)):
            self.assertEqual(paths.source_repo(), str(self.repo))
            self.assertEqual(os.path.normcase(os.path.normpath(patch.git('rev-parse', '--show-toplevel'))), os.path.normcase(str(self.repo)))

    def test_cached_git_metadata_never_becomes_editable_source(self):
        subprocess.run(['git', 'clone', '-q', str(self.repo), str(self.cache)], check=True, capture_output=True)
        store.write_json(paths.path('synced.json'), {'source_repo': str(self.repo)})
        with mock.object(paths, 'REPO', str(self.cache)):
            self.assertEqual(paths.source_repo(), str(self.repo))
            self.assertEqual(os.path.normcase(os.path.normpath(patch.git('rev-parse', '--show-toplevel'))), os.path.normcase(str(self.repo)))
        for metadata in ({}, {'source_repo': str(self.cache)}, {'source_repo': str(self.root / 'missing')}):
            store.write_json(paths.path('synced.json'), metadata)
            with mock.object(paths, 'REPO', str(self.cache)):
                with self.assertRaises(RuntimeError):
                    paths.source_repo()

    def test_cached_source_requires_honey_checkout(self):
        for candidate in (str(self.cache), 'relative/path', str(self.root / 'missing')):
            store.write_json(paths.path('synced.json'), {'source_repo': candidate})
            with mock.object(paths, 'REPO', str(self.cache)):
                with self.assertRaises(RuntimeError):
                    paths.source_repo()
        (self.repo / '.codex-plugin' / 'plugin.json').write_text('{"name":"unrelated"}', encoding="utf-8")
        store.write_json(paths.path('synced.json'), {'source_repo': str(self.repo)})
        with mock.object(paths, 'REPO', str(self.cache)):
            with self.assertRaises(RuntimeError):
                paths.source_repo()

    def test_git_file_worktree_is_a_source(self):
        wt = self.root / 'worktree'
        self.git('worktree', 'add', '-q', '-b', 'test-worktree', str(wt))
        store.write_json(paths.path('synced.json'), {'source_repo': str(wt)})
        with mock.object(paths, 'REPO', str(self.cache)):
            self.assertEqual(paths.source_repo(), str(wt))

    def test_sync_records_success_source_and_head_only(self):
        env = install.Env()
        prior = {'source_repo': '/old/source', 'head': 'old'}
        store.write_json(paths.path('synced.json'), prior)
        for rc in (1, 0):
            def run(args, **kwargs):
                if args[0] == 'git':
                    return subprocess.CompletedProcess(args, 0, 'abc123\n', '')
                self.assertEqual(args, ['/fake/codex', 'plugin', 'add', install.PLUGIN])
                return subprocess.CompletedProcess(args, rc, 'fake plugin response', '')
            with mock.object(paths, 'source_repo', return_value=str(self.repo)), mock.object(install, '_cli', return_value='/fake/codex'), mock.object(install.subprocess, 'run', side_effect=run):
                self.assertEqual(install.sync(env), rc)
            saved = store.read_json(paths.path('synced.json'))
            self.assertEqual(saved, prior if rc else {'ts': env.stamp, 'head': 'abc123', 'source_repo': str(self.repo)})


if __name__ == '__main__':
    unittest.main()
