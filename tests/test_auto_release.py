"""Automatic releases against real disposable Git remotes; GitHub is a fixture."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import REPO
sys.path.insert(0, str(Path(REPO, 'scripts')))
import auto_release
release = auto_release.release


class VersionSelection(unittest.TestCase):
    def test_semantic_version_from_changes(self):
        self.assertEqual(release.next_version('1.2.3', ['fix: correct output']), '1.2.4')
        self.assertEqual(release.next_version('1.2.3', ['feat(rsi): publish suggestions']), '1.3.0')
        self.assertEqual(release.next_version('1.2.3', ['feat!: change CLI']), '2.0.0')
        self.assertEqual(release.next_version('1.2.3', ['fix: new format\n\nBREAKING CHANGE: old data unsupported']), '2.0.0')
        self.assertEqual(release.next_version('1.2.3', ['docs: update readme']), '1.2.4')
        self.assertEqual(release.next_version('1.2.3', ['feat: feature', 'fix!: new format']), '2.0.0')


class AutomaticRelease(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, 'source'); self.root.mkdir()
        self.remote = Path(self.tmp.name, 'remote.git')
        self.addCleanup(patch.stopall)
        patch.object(release, 'ROOT', str(self.root)).start()
        patch.dict(os.environ, GITHUB_REPOSITORY='example/honey').start()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.test')
        self.write('honey/__init__.py', '__version__ = "1.2.3"\n')
        for name in release.JSON_FILES:
            self.write(name, json.dumps({'name': 'honey', 'version': '1.2.3'}))
        self.write('CHANGELOG.md', '# Changelog\n\n## [Unreleased]\n\n### Added\n- A feature.\n\n'
                   '## [1.2.3] - 2026-01-01\n\nOld release.\n\n[Unreleased]: '
                   'https://github.com/umararshad4/honey-stack/compare/v1.2.3...HEAD\n')
        self.write('tests/run.py', "print('synthetic release-test suite passed')\n")
        self.git('add', '.'); self.git('commit', '-qm', 'baseline'); self.git('tag', 'v1.2.3')
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True)
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-qu', 'origin', 'main', '--tags')
        self.write('feature.txt', 'feature\n')
        self.git('add', 'feature.txt'); self.git('commit', '-qm', 'feat: add a feature')
        self.git('push', '-q', 'origin', 'main')
        self.expected = self.git('rev-parse', 'HEAD')
        self.releases = []
        self.created = []
        self.api_failure = False
        self.create_failure = False
        real = auto_release.command
        def command(*args):
            if args[0] != 'gh':
                return real(*args)
            if args[1] == 'api':
                if self.api_failure:
                    raise RuntimeError('GitHub authentication failed')
                return json.dumps([self.releases])
            if args[1:3] == ('release', 'create'):
                if self.create_failure:
                    raise RuntimeError('GitHub temporarily unavailable')
                tag = args[3]
                notes = Path(args[args.index('--notes-file') + 1]).read_text(encoding="utf-8")
                self.assertTrue(notes.strip())
                url = 'https://github.com/example/honey/releases/tag/' + tag
                self.releases.append({'tag_name': tag, 'html_url': url, 'draft': False})
                self.created.append(tag)
                return url
            raise AssertionError(args)
        patch.object(auto_release, 'command', side_effect=command).start()

    def write(self, name, content):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content, encoding="utf-8")

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()

    def test_release_pushes_consistent_metadata_and_tag_then_publishes(self):
        result = auto_release.run(self.expected)
        self.assertEqual(result['tag'], 'v1.3.0')
        self.assertEqual(set(release.versions().values()), {'1.3.0'})
        self.assertEqual(self.git('rev-parse', 'v1.3.0^'), self.expected)
        self.assertIn('Release-Source: ' + self.expected, self.git('log', '-1', '--format=%B'))
        remote = self.git('ls-remote', 'origin', 'refs/heads/main').split()[0]
        self.assertEqual(remote, self.git('rev-parse', 'HEAD'))
        self.assertIn('refs/tags/v1.3.0', self.git('ls-remote', 'origin', 'refs/tags/v1.3.0'))
        self.assertEqual(self.created, ['v1.3.0'])
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_rerun_does_not_bump_or_publish_twice(self):
        auto_release.run(self.expected)
        head = self.git('rev-parse', 'HEAD')
        again = auto_release.run(self.expected)
        self.assertTrue(again['recovered'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.created, ['v1.3.0'])

    def test_release_commit_does_not_start_another_version(self):
        auto_release.run(self.expected)
        head = self.git('rev-parse', 'HEAD')
        result = auto_release.run(head)
        self.assertEqual(result['tag'], 'v1.3.0')
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.created, ['v1.3.0'])

    def test_failed_publication_recovers_existing_tag(self):
        self.create_failure = True
        with self.assertRaisesRegex(RuntimeError, 'temporarily unavailable'):
            auto_release.run(self.expected)
        head = self.git('rev-parse', 'HEAD')
        self.create_failure = False
        self.assertTrue(auto_release.run(self.expected)['recovered'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.created, ['v1.3.0'])

    def test_lost_publish_response_recovers_without_duplicate(self):
        real = auto_release.command
        def lose_response(*args):
            result = real(*args)
            if args[:3] == ('gh', 'release', 'create'):
                raise RuntimeError('response lost after creation')
            return result
        with patch.object(auto_release, 'command', side_effect=lose_response):
            with self.assertRaisesRegex(RuntimeError, 'response lost'):
                auto_release.run(self.expected)
        auto_release.run(self.expected)
        self.assertEqual(self.created, ['v1.3.0'])

    def test_invalid_ci_sha_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'full commit SHA'):
            auto_release.run('main; echo unsafe')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.expected)

    def test_version_mismatch_blocks_before_mutation(self):
        self.write('honey/__init__.py', '__version__ = "1.2.4"\n')
        self.git('commit', '-qam', 'bad version'); self.git('push', '-q', 'origin', 'main')
        with self.assertRaisesRegex(ValueError, 'metadata does not match'):
            auto_release.run(self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.created, [])

    def test_failed_auth_is_not_treated_as_missing_release(self):
        self.api_failure = True
        with self.assertRaisesRegex(RuntimeError, 'authentication failed'):
            auto_release.run(self.expected)
        self.assertEqual(self.created, [])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.expected)
        self.assertEqual(self.git('tag', '--list'), 'v1.2.3')

    def test_missing_repository_blocks_before_mutation(self):
        with patch.dict(os.environ, GITHUB_REPOSITORY=''):
            with self.assertRaisesRegex(ValueError, 'GITHUB_REPOSITORY'):
                auto_release.run(self.expected)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.expected)

    def test_tests_mutating_source_block_release(self):
        self.write('tests/run.py', 'from pathlib import Path\nPath("feature.txt").write_text("modified", encoding="utf-8")\n')
        self.git('commit', '-qam', 'test: change source'); self.git('push', '-q', 'origin', 'main')
        expected = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'outside release metadata'):
            auto_release.run(expected)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main').split()[0], expected)
        self.assertEqual(self.git('tag', '--list'), 'v1.2.3')

    def test_tests_committing_source_block_release(self):
        self.write('tests/run.py', '''from pathlib import Path
import subprocess
Path("feature.txt").write_text("changed during tests", encoding="utf-8")
subprocess.run(["git", "add", "feature.txt"], check=True)
subprocess.run(["git", "commit", "-qm", "unverified change"], check=True)
''')
        self.git('commit', '-qam', 'test: committed mutation'); self.git('push', '-q', 'origin', 'main')
        expected = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'HEAD changed during tests'):
            auto_release.run(expected)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main').split()[0], expected)
        self.assertEqual(self.git('tag', '--list'), 'v1.2.3')
        self.assertEqual(self.created, [])

    def test_stale_ci_is_skipped(self):
        self.write('later.txt', 'later\n'); self.git('add', 'later.txt')
        self.git('commit', '-qm', 'fix: later merge'); self.git('push', '-q', 'origin', 'main')
        result = auto_release.run(self.expected)
        self.assertIn('skipped', result)
        self.assertEqual(self.git('tag', '--list'), 'v1.2.3')
        self.assertEqual(self.created, [])

    def test_failed_tests_never_push_metadata_or_tag(self):
        self.write('tests/run.py', 'raise SystemExit(1)\n')
        self.git('commit', '-qam', 'test: fail'); self.git('push', '-q', 'origin', 'main')
        expected = self.git('rev-parse', 'HEAD')
        with self.assertRaises(subprocess.CalledProcessError):
            auto_release.run(expected)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main').split()[0], expected)
        self.assertEqual(self.git('tag', '--list'), 'v1.2.3')
        self.assertEqual(self.created, [])

    def test_atomic_push_rejects_racing_main_without_orphan_tag(self):
        import shlex
        tree = self.git('rev-parse', 'HEAD^{tree}')
        other = self.git('commit-tree', tree, '-p', self.expected, '-m', 'fix: concurrent merge')
        self.git('push', '-q', 'origin', other + ':refs/heads/concurrent')
        hook = self.root / '.git/hooks/pre-push'
        hook.write_text('#!/bin/sh\ngit --git-dir=' + shlex.quote(str(self.remote)) +
                        ' update-ref refs/heads/main ' + other + '\n')
        hook.chmod(0o755)
        with self.assertRaises(RuntimeError):
            auto_release.run(self.expected)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main').split()[0], other)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/tags/v1.3.0'), '')
        self.assertEqual(self.created, [])

    def test_dirty_checkout_and_wrong_branch_are_rejected(self):
        self.write('private.txt', 'local')
        with self.assertRaisesRegex(ValueError, 'clean checkout'):
            auto_release.run(self.expected)
        (self.root / 'private.txt').unlink()
        self.git('switch', '-qc', 'feature')
        with self.assertRaisesRegex(ValueError, 'main branch'):
            auto_release.run(self.expected)

    def test_empty_unreleased_uses_commit_subjects(self):
        self.write('CHANGELOG.md', '# Changelog\n\n## [Unreleased]\n\n## [1.2.3] - 2026-01-01\nOld\n')
        self.git('commit', '-qam', 'docs: update changelog'); self.git('push', '-q', 'origin', 'main')
        auto_release.run(self.git('rev-parse', 'HEAD'))
        self.assertIn('feat: add a feature', release.notes('1.3.0'))

    def test_manual_release_cli_still_bumps_and_tags(self):
        release.main(['1.3.0'])
        self.assertEqual(set(release.versions().values()), {'1.3.0'})
        self.assertEqual(self.git('rev-parse', 'v1.3.0^{commit}'), self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/tags/v1.3.0'), '')


if __name__ == '__main__':
    unittest.main()
