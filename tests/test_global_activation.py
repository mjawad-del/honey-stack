"""Global instruction fallback loads real rules without inventing hook events."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from helpers import REPO
from honey import __version__, activation, install


class GlobalActivation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = install.Env(str(self.root))
        self.agent_file = self.root / '.codex/AGENTS.md'
        self.claude_file = self.root / '.claude/CLAUDE.md'
        for path in (self.agent_file, self.claude_file):
            path.parent.mkdir(parents=True)
            path.write_text('# Personal preferences\nKeep my existing instructions.\n', encoding="utf-8")

    def test_install_preserves_personal_instructions_and_adds_idempotent_activation(self):
        original = self.agent_file.read_text(encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install.install(self.env, False, True), 0)
        for path, agent in ((self.agent_file, 'codex'), (self.claude_file, 'claude')):
            body = path.read_text(encoding="utf-8")
            self.assertTrue(body.startswith(original))
            self.assertIn('honey:global:start', body)
            self.assertIn('bootstrap --agent ' + agent, body)
            self.assertIn('if no native `honey-stack active` startup context was supplied', body)
            self.assertIn('hooks unverified', body)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install.install(self.env, False, True), 0)
        self.assertEqual(self.agent_file.read_text(encoding="utf-8").count('honey:global:start'), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install.uninstall(self.env, False, True, False), 0)
        self.assertEqual(self.agent_file.read_text(encoding="utf-8"), original)
        self.assertEqual(self.claude_file.read_text(encoding="utf-8"), original)

    def test_bootstrap_is_read_only_and_marks_unobserved_hooks(self):
        home = self.root / '.honey'
        home.mkdir()
        (home / 'config.json').write_text(json.dumps({'mode': 'explain'}), encoding="utf-8")
        env = dict(os.environ, HONEY_HOME=str(home), CODEX_THREAD_ID='probe')
        before = {str(p.relative_to(home)): p.read_bytes() for p in home.rglob('*') if p.is_file()}
        result = subprocess.run([sys.executable, str(Path(REPO, 'bin/honey')), 'bootstrap', '--agent', 'codex'],
                                env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('🍯 HONEY · explain · hooks unverified', result.stdout)
        self.assertIn('Smallest complete change', result.stdout)
        after = {str(p.relative_to(home)): p.read_bytes() for p in home.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_uninstall_preserves_later_personal_edits(self):
        with contextlib.redirect_stdout(io.StringIO()):
            install.install(self.env, False, True)
        self.agent_file.write_text(self.agent_file.read_text(encoding="utf-8") + '\nLater personal instruction.\n', encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            install.uninstall(self.env, False, True, False)
        self.assertIn('Later personal instruction.', self.agent_file.read_text(encoding="utf-8"))
        self.assertIn('Keep my existing instructions.', self.agent_file.read_text(encoding="utf-8"))
        self.assertNotIn('honey:global:start', self.agent_file.read_text(encoding="utf-8"))

    def test_codex_only_install_provides_the_bootstrap_launcher(self):
        env = install.Env(str(self.root), only='codex')
        with contextlib.redirect_stdout(io.StringIO()):
            install.install(env, False, True)
        launcher = self.root / '.local/bin/honey'
        self.assertTrue(launcher.is_file())
        self.assertIn('bootstrap --agent codex', self.agent_file.read_text(encoding="utf-8"))
        self.assertNotIn('honey:global:start', self.claude_file.read_text(encoding="utf-8"))

    def test_malformed_session_state_keeps_bootstrap_available(self):
        home = self.root / '.honey'
        state = home / 'state/sessions/codex-probe.json'
        state.parent.mkdir(parents=True)
        with patch.dict(os.environ, HONEY_HOME=str(home), CODEX_THREAD_ID='probe'):
            for value in (None, [], True):
                state.write_text(json.dumps(value), encoding="utf-8")
                self.assertIn('hooks unverified', activation.context('codex'))
                self.assertEqual(json.loads(state.read_text(encoding="utf-8")), value)

    def test_recent_heartbeat_with_disabled_plugin_or_old_runtime_is_unverified(self):
        home = self.root / '.honey'
        state = home / 'state/sessions/codex-probe.json'
        state.parent.mkdir(parents=True)
        config = self.root / '.codex/config.toml'
        with patch.dict(os.environ, HONEY_HOME=str(home), CODEX_HOME=str(self.root / '.codex'), CODEX_THREAD_ID='probe'):
            for enabled, version in ((False, __version__), (True, '0.0.0')):
                config.write_text('[plugins."honey@honey-stack"]\nenabled = ' + str(enabled).lower() + '\n', encoding="utf-8")
                state.write_text(json.dumps({'health': {'last_seen': time.time(), 'last_event': 'SessionStart',
                                                       'runtime_version': version}}))
                self.assertIn('hooks unverified', activation.context('codex'))


if __name__ == '__main__':
    unittest.main()
