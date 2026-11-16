"""Verification executes real commands and binds proof to project content."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import REPO, hook, TempHome


class VerificationRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = TempHome(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project = root / 'project'; self.project.mkdir()
        self.home = root / 'state'
        self.patch = patch.dict(os.environ, HONEY_HOME=str(self.home), HONEY_NO_AUTOUPDATE='1')
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test'); self.git('config', 'user.email', 'test@example.invalid')
        (self.project / 'tests').mkdir()
        (self.project / 'tests/check.py').write_text('print("actual check passed")\n', encoding="utf-8")
        (self.project / 'source.py').write_text('value = 1\n', encoding="utf-8")
        self.git('add', '.'); self.git('commit', '-qm', 'fixture')
        self.event('UserPromptSubmit', prompt='Fix the source')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.project), *args], text=True, encoding='utf-8').strip()

    def event(self, name, **fields):
        return hook('codex', dict(session_id='runner', cwd=str(self.project), hook_event_name=name, **fields))

    def cli(self, *args):
        return subprocess.run([sys.executable, str(Path(REPO, 'bin/honey')), 'task', *args],
                              cwd=self.project, text=True, encoding="utf-8", capture_output=True)

    def run_check(self):
        return self.cli('run', '--agent', 'codex', '--session', 'runner', '--check', 'verification',
                        '--', sys.executable, 'tests/check.py')

    def status(self):
        result = self.cli('status', '--agent', 'codex', '--session', 'runner')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_real_check_survives_opaque_wrapper_only_while_source_matches(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('actual check passed', result.stdout)
        self.event('PostToolUse', tool_name='functions.exec', tool_input={'code': 'opaque host wrapper'},
                   tool_response={'output': 'untrusted text'}, tool_use_id='outer')
        self.assertEqual(self.status()['missing_checks'], [])
        (self.project / 'source.py').write_text('value = 2\n', encoding="utf-8")
        self.assertIn('verification', self.status()['missing_checks'])

    def test_failed_rerun_cannot_reuse_success(self):
        self.assertEqual(self.run_check().returncode, 0)
        (self.project / 'tests/check.py').write_text('raise SystemExit(1)\n', encoding="utf-8")
        self.assertNotEqual(self.run_check().returncode, 0)
        self.assertIn('verification', self.status()['missing_checks'])

    def test_opaque_progress_event_during_long_check_does_not_destroy_proof(self):
        import time
        marker = self.home / 'check-started'
        (self.project / 'tests/check.py').write_text(
            'from pathlib import Path\nimport time\nPath(' + repr(str(marker)) + ').touch()\ntime.sleep(0.5)\n')
        process = subprocess.Popen([sys.executable, str(Path(REPO, 'bin/honey')), 'task', 'run',
            '--agent', 'codex', '--session', 'runner', '--check', 'verification', '--',
            sys.executable, 'tests/check.py'], cwd=self.project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            self.event('PostToolUse', tool_name='functions.exec', tool_input={'code': 'progress'},
                       tool_response={}, tool_use_id='progress')
            output, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, output + error)
            self.assertEqual(self.status()['missing_checks'], [])
        finally:
            if process.poll() is None:
                process.kill(); process.communicate()

    def test_command_that_changes_source_cannot_verify_itself(self):
        (self.project / 'tests/check.py').write_text('from pathlib import Path\nPath("source.py").write_text("changed")\n', encoding="utf-8")
        self.assertNotEqual(self.run_check().returncode, 0)
        self.assertIn('verification', self.status()['missing_checks'])

    def test_arbitrary_command_and_inspection_task_are_rejected(self):
        result = self.cli('run', '--agent', 'codex', '--session', 'runner', '--check', 'verification',
                          '--', 'touch', 'unexpected')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.project / 'unexpected').exists())
        self.event('UserPromptSubmit', prompt='Only inspect the source')
        self.assertNotEqual(self.run_check().returncode, 0)

    def test_untrusted_wrapper_output_cannot_create_evidence(self):
        self.event('PostToolUse', tool_name='functions.exec', tool_input={'code': 'print("passed")'},
                   tool_response={'exit_code': 0}, tool_use_id='fake')
        self.assertIn('verification', self.status()['missing_checks'])

    def test_failed_wrapper_invalidates_real_check(self):
        self.assertEqual(self.run_check().returncode, 0)
        self.event('PostToolUseFailure', tool_name='functions.exec', tool_input={'code': 'opaque'},
                   error='failed', tool_use_id='outer-failed')
        self.assertIn('verification', self.status()['missing_checks'])

    def test_committing_source_invalidates_real_check(self):
        self.assertEqual(self.run_check().returncode, 0)
        self.git('commit', '--allow-empty', '-qm', 'new revision')
        self.assertIn('verification', self.status()['missing_checks'])

    def test_record_can_be_linked_and_allows_stop_after_wrapper(self):
        self.assertEqual(self.run_check().returncode, 0)
        ident = self.status()['task']['evidence']['verification']['id']
        self.event('PostToolUse', tool_name='functions.exec', tool_input={'code': 'opaque'},
                   tool_response={}, tool_use_id='outer')
        result = self.cli('verify', '--agent', 'codex', '--session', 'runner',
                          '--check', 'verification', '--tool-call', ident)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.event('Stop', last_assistant_message='Fixed. Checks run: real check passed.'), {})

    def test_make_request_exits_inspection_mode(self):
        self.event('UserPromptSubmit', prompt='Only inspect the source')
        self.event('UserPromptSubmit', prompt='bro make it like that')
        self.assertEqual(self.status()['task']['mode'], 'execute')
        self.event('UserPromptSubmit', prompt='Make recommendations only')
        self.assertEqual(self.status()['task']['mode'], 'inspect')

    def test_new_prompt_during_final_snapshot_is_not_overwritten(self):
        import threading
        from honey import dispatch, verification
        original = verification.snapshot
        calls = 0
        threads = []
        def snapshot(cwd):
            nonlocal calls
            calls += 1
            value = original(cwd)
            if calls == 2:
                done = threading.Event()
                def next_prompt():
                    dispatch.handle('codex', {'session_id': 'runner', 'cwd': str(self.project),
                        'hook_event_name': 'UserPromptSubmit', 'prompt': 'Fix the next task'})
                    done.set()
                thread = threading.Thread(target=next_prompt)
                threads.append(thread); thread.start()
                done.wait(0.3)
            return value
        state_file = self.home / 'state/sessions/codex-runner.json'
        with patch.object(verification, 'snapshot', side_effect=snapshot):
            verification.run(str(state_file), 'verification', [sys.executable, 'tests/check.py'])
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.status()['task']['request'], 'Fix the next task')
        self.assertFalse(self.status()['task']['evidence'])

    def test_snapshot_covers_symlinks_subfolders_and_worktrees(self):
        from honey import verification
        (self.project / 'link').symlink_to('/etc/hosts')
        (self.project / 'pkg').mkdir(); (self.project / 'pkg/mod.py').write_text('x = 1\n', encoding="utf-8")
        self.git('add', '.'); self.git('commit', '-qm', 'link')
        root = verification.snapshot(str(self.project))
        self.assertEqual(verification.snapshot(str(self.project / 'pkg')), root)
        tree = Path(self.tmp.name, 'tree')
        self.git('worktree', 'add', '-q', str(tree))
        before = verification.snapshot(str(tree))
        (tree / 'pkg/mod.py').write_text('x = 2\n', encoding="utf-8")
        self.assertNotEqual(verification.snapshot(str(tree / 'pkg')), before)


    def test_snapshot_tracks_every_kind_of_change_without_writing_to_git(self):
        from honey import verification
        snap = lambda: verification.snapshot(str(self.project))
        (self.project / '.gitignore').write_text('build/\n', encoding="utf-8")
        (self.project / 'dir with space').mkdir()
        (self.project / 'dir with space/naïve.py').write_text('a = 1\n', encoding="utf-8")
        (self.project / 'link').symlink_to('source.py')
        self.git('add', '.'); self.git('commit', '-qm', 'more')
        objects = lambda: sorted(p.name for p in (self.project / '.git/objects').rglob('*') if p.is_file())
        index = self.project / '.git/index'
        seen, index_stat, before_objects = {snap()}, index.stat().st_mtime_ns, objects()

        def changed(label):
            digest = snap()
            self.assertNotIn(digest, seen, label)
            seen.add(digest)

        src = self.project / 'source.py'
        stat = src.stat()
        src.write_text('value = 2\n', encoding="utf-8")            # same size, mtime restored: ctime still moves
        os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns)); changed('same-size edit with restored mtime')
        (self.project / 'dir with space/naïve.py').write_text('a = 22\n', encoding="utf-8"); changed('unicode path edit')
        (self.project / 'new.txt').write_text('n\n', encoding="utf-8"); changed('untracked file')
        (self.project / 'new.txt').write_text('m\n', encoding="utf-8"); changed('untracked edit')
        (self.project / 'link').unlink(); (self.project / 'link').symlink_to('tests'); changed('symlink retarget')
        (self.project / 'tests/check.py').unlink(); changed('tracked deletion')
        self.assertEqual(index.stat().st_mtime_ns, index_stat, 'snapshot must not rewrite .git/index')
        self.assertEqual(objects(), before_objects, 'snapshot must not write git objects')
        self.git('add', 'new.txt'); changed('staged')
        self.git('mv', 'source.py', 'moved.py'); changed('rename')
        current = snap()
        (self.project / 'build').mkdir(); (self.project / 'build/out.bin').write_bytes(b'x' * 10)
        self.assertEqual(snap(), current, 'ignored files do not count')
        if os.name != 'nt':  # Windows hashes every file with its timestamps (ctime there is creation time)
            os.utime(self.project / '.gitignore'); self.assertEqual(snap(), current, 'touching a clean file is not a change')
        self.git('commit', '-qm', 'next'); changed('commit')


if __name__ == '__main__':
    unittest.main()
