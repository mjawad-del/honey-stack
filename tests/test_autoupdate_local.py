"""Exercise a cached install whose marketplace points at a local Git checkout."""
import json
import os
import contextlib
import io
import subprocess
import sys
import tempfile
import shutil
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from honey import autoupdate, cli, dispatch, install, paths  # noqa: E402
from honey.rsi import store  # noqa: E402

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
       "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]


@unittest.skipIf(os.name == 'nt', 'fake codex CLI fixture is an extensionless script; Windows only runs .exe/.cmd')
class LocalMarketplaceUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "honey-home"
        self.home.mkdir()
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.git(self.origin, "init", "-q", "-b", "main")
        (self.origin / ".codex-plugin").mkdir()
        (self.origin / "honey").mkdir()
        (self.origin / ".codex-plugin" / "plugin.json").write_text('{"name":"honey","version":"1.0.0"}', encoding="utf-8")
        (self.origin / "honey" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
        self.git(self.origin, "add", ".codex-plugin/plugin.json", "honey/__init__.py")
        self.git(self.origin, "commit", "-qm", "first release")
        self.git(self.origin, "tag", "v1.0.0")
        self.checkout = self.root / "source checkout"
        subprocess.run([*GIT, "clone", "-q", str(self.origin), str(self.checkout)], check=True)
        self.old_head = self.git(self.checkout, "rev-parse", "HEAD")
        self.cache = self.root / "codex" / "plugins" / "cache" / "honey-stack" / "honey" / "1.0.0"
        self.cache.mkdir(parents=True)
        (self.root / "codex" / "config.toml").write_text('[marketplaces.honey-stack]\nsource_type = "local"\n', encoding="utf-8")
        self.codex = self.root / "codex-cli"
        self.codex.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["TEST_UPDATE_ROOT"])
args = sys.argv[1:]
if args == ["plugin", "marketplace", "list"]:
    print("MARKETPLACE ROOT\\nhoney-stack " + str(root / "source checkout"))
elif args == ["plugin", "add", "honey@honey-stack"]:
    if (root / "fail-refresh").exists():
        sys.exit(1)
    source = root / "source checkout"
    if pathlib.Path.cwd().resolve() != source.resolve():
        sys.exit(3)
    version = json.loads((source / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    cache = root / "codex" / "plugins" / "cache" / "honey-stack" / "honey" / version
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "installed-version").write_text(version, encoding="utf-8")
else:
    sys.exit(2)
''')
        self.codex.chmod(0o755)
        self.env = patch.dict(os.environ, {"HONEY_HOME": str(self.home), "TEST_UPDATE_ROOT": str(self.root),
                                           "CODEX_HOME": str(self.root / "codex"),
                                           "CLAUDE_CONFIG_DIR": str(self.root / "claude")}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("HONEY_NO_AUTOUPDATE", None)
        self.git_env = patch.object(autoupdate, "GIT_ENV", dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        self.git_env.start()
        self.addCleanup(self.git_env.stop)
        self.repo = patch.object(paths, "REPO", str(self.cache))
        self.repo.start()
        self.addCleanup(self.repo.stop)
        self.cli = patch.object(install, "_cli", side_effect=lambda agent: str(self.codex) if agent == "codex" else None)
        self.cli.start()
        self.addCleanup(self.cli.stop)
        self.override = patch.object(autoupdate, "REPO_OVERRIDE", str(self.cache))
        self.override.start()
        self.addCleanup(self.override.stop)
        store.write_json(paths.path("synced.json"), {"source_repo": str(self.checkout), "head": self.old_head})

    def git(self, cwd, *args):
        return subprocess.run([*GIT, "-C", str(cwd), *args], check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()

    def release(self):
        (self.origin / ".codex-plugin" / "plugin.json").write_text('{"name":"honey","version":"1.1.0"}', encoding="utf-8")
        (self.origin / "honey" / "__init__.py").write_text('__version__ = "1.1.0"\n', encoding="utf-8")
        self.git(self.origin, "add", ".codex-plugin/plugin.json", "honey/__init__.py")
        self.git(self.origin, "commit", "-qm", "second release")
        self.git(self.origin, "tag", "v1.1.0")
        return self.git(self.origin, "rev-parse", "HEAD")

    def manual_update(self, source=None):
        out = io.StringIO()
        with patch.object(paths, "REPO", str(source or self.checkout)), contextlib.redirect_stdout(out):
            code = install.update(install.Env())
        return code, out.getvalue()

    def test_manual_update_from_detached_release_refreshes_cache(self):
        self.git(self.checkout, "checkout", "-q", "--detach", "v1.0.0")
        new_head = self.release()
        code, out = self.manual_update()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), new_head)
        self.assertEqual((self.cache.parent / "1.1.0" / "installed-version").read_text(encoding="utf-8"), "1.1.0")

    def test_manual_update_never_installs_unreleased_main(self):
        release_head = self.release()
        (self.origin / "unreleased.txt").write_text("not released", encoding="utf-8")
        self.git(self.origin, "add", "unreleased.txt")
        self.git(self.origin, "commit", "-qm", "unreleased main")
        code, out = self.manual_update()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), release_head)
        self.assertFalse((self.checkout / "unreleased.txt").exists())

    def test_manual_update_does_not_refresh_an_unreleased_checkout(self):
        (self.origin / "unreleased.txt").write_text("not released", encoding="utf-8")
        self.git(self.origin, "add", "unreleased.txt")
        self.git(self.origin, "commit", "-qm", "unreleased main")
        self.git(self.checkout, "pull", "--ff-only", "-q")
        head = self.git(self.checkout, "rev-parse", "HEAD")
        code, out = self.manual_update()
        self.assertNotEqual(code, 0, out)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertFalse((self.cache / "installed-version").exists())
        self.assertIn("release tag", out)

    def test_manual_update_resolves_source_from_installed_cache(self):
        new_head = self.release()
        code, out = self.manual_update(self.cache)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), new_head)
        self.assertEqual(store.read_json(paths.path("synced.json"))["head"], new_head)

    def test_manual_update_reports_cache_refresh_failure(self):
        self.release()
        (self.root / "fail-refresh").touch()
        code, out = self.manual_update()
        self.assertNotEqual(code, 0, out)
        self.assertEqual(store.read_json(paths.path("synced.json"))["head"], self.old_head)

    def test_manual_update_preserves_clean_feature_branch(self):
        self.git(self.checkout, "switch", "-q", "-c", "feature")
        self.release()
        code, out = self.manual_update()
        self.assertNotEqual(code, 0, out)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.old_head)
        self.assertIn("skipped", out)

    def test_tag_only_fetch_configuration_still_finds_new_release(self):
        self.git(self.checkout, "config", "remote.origin.fetch", "+refs/tags/v1.0.0:refs/tags/v1.0.0")
        new_head = self.release()
        result = autoupdate.update_checkout(str(self.checkout))
        self.assertEqual(result["status"], "updated", result)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), new_head)

    def test_release_updates_checkout_and_installed_cache(self):
        new_head = self.release()
        result = autoupdate.run()
        self.assertEqual(result.get("status"), "updated", result)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), new_head)
        self.assertEqual((self.cache.parent / "1.1.0" / "installed-version").read_text(encoding="utf-8"), "1.1.0")
        self.assertEqual(store.read_json(paths.path("synced.json"))["head"], new_head)
        self.assertIn("1.0.0 → 1.1.0", autoupdate.notice())

    def test_explicit_check_finds_release_despite_recent_automatic_check(self):
        self.assertEqual(autoupdate.run()['status'], 'current')
        new_head = self.release()
        result = autoupdate.run(force=True)
        self.assertEqual(result.get('status'), 'updated', result)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), new_head)
        self.assertEqual((self.cache.parent / '1.1.0' / 'installed-version').read_text(encoding="utf-8"), '1.1.0')

    def test_cli_exposes_explicit_release_check(self):
        with patch.object(autoupdate, 'run', return_value={'status': 'current'}) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['autoupdate', '--now']), 0)
        run.assert_called_once_with(force=True)

    def test_cli_now_updates_a_local_marketplace_after_recent_check(self):
        source_root = Path(__file__).resolve().parents[1]
        shutil.copytree(source_root / 'honey', self.cache / 'honey', ignore=shutil.ignore_patterns('__pycache__'))
        (self.cache / 'bin').mkdir()
        shutil.copy2(source_root / 'bin' / 'honey', self.cache / 'bin' / 'honey')
        toolbin = self.root / 'tools'
        toolbin.mkdir()
        shutil.copy2(self.codex, toolbin / 'codex')
        store.write_json(paths.path('state', 'autoupdate.json'), {'checked': time.time()})
        self.release()
        env = dict(os.environ, PATH=str(toolbin) + os.pathsep + os.environ.get('PATH', ''))
        result = subprocess.run([sys.executable, str(self.cache / 'bin' / 'honey'), 'autoupdate', '--now'],
                                cwd=self.root, env=env, capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(result.stdout)['to'], '1.1.0')
        self.assertEqual((self.cache.parent / '1.1.0' / 'installed-version').read_text(encoding="utf-8"), '1.1.0')

    def test_explicit_check_does_not_run_alongside_an_update(self):
        lockfile = Path(paths.path('state', 'autoupdate.lock'))
        lockfile.parent.mkdir(parents=True, exist_ok=True)
        from honey import filelock
        with filelock.exclusive(str(lockfile)):
            result = autoupdate.run(force=True)
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('in progress', result['reason'])

    def test_explicit_check_refreshes_cache_when_checkout_is_current(self):
        result = autoupdate.run(force=True)
        self.assertEqual(result.get('status'), 'current', result)
        self.assertTrue(result.get('refreshed'), result)
        self.assertEqual((self.cache / 'installed-version').read_text(encoding="utf-8"), '1.0.0')
        self.assertEqual(autoupdate.notice(), '')

    def test_explicit_check_does_not_install_unreleased_main(self):
        (self.origin / 'unreleased.txt').write_text('unreleased', encoding="utf-8")
        self.git(self.origin, 'add', 'unreleased.txt')
        self.git(self.origin, 'commit', '-qm', 'unreleased main')
        self.git(self.checkout, 'pull', '--ff-only', '-q')
        result = autoupdate.run(force=True)
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('release tag', result['reason'])
        self.assertFalse((self.cache / 'installed-version').exists())

    def test_explicit_check_does_not_refresh_an_older_tagged_release(self):
        (self.origin / '.codex-plugin' / 'plugin.json').write_text('{"name":"honey","version":"0.9.0"}', encoding="utf-8")
        (self.origin / 'honey' / '__init__.py').write_text('__version__ = "0.9.0"\n', encoding="utf-8")
        self.git(self.origin, 'add', '.codex-plugin/plugin.json', 'honey/__init__.py')
        self.git(self.origin, 'commit', '-qm', 'older version on main')
        self.git(self.origin, 'tag', 'v0.9.0')
        self.git(self.checkout, 'pull', '--ff-only', '-q', '--tags')
        result = autoupdate.run(force=True)
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('release tag', result['reason'])
        self.assertFalse((self.cache / 'installed-version').exists())

    def test_explicit_check_respects_auto_update_opt_out(self):
        store.write_json(paths.path('config.json'), {'autoupdate': False})
        self.release()
        result = autoupdate.run(force=True)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), self.old_head)

    def test_explicit_check_does_not_update_a_dirty_checkout(self):
        self.release()
        (self.checkout / 'honey' / '__init__.py').write_text('# local edit\n', encoding="utf-8")
        result = autoupdate.run(force=True)
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('local changes', result['reason'])
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), self.old_head)
        self.assertFalse((self.cache.parent / '1.1.0').exists())

    def test_current_release_refresh_failure_remains_retryable(self):
        (self.root / 'fail-refresh').touch()
        result = autoupdate.run(force=True)
        self.assertEqual(result['status'], 'skipped')
        self.assertTrue(store.read_json(paths.path('state', 'autoupdate.json'))['pending_refresh']['refresh_only'])
        (self.root / 'fail-refresh').unlink()
        state = store.read_json(paths.path('state', 'autoupdate.json'))
        state['checked'] = 0
        store.write_json(paths.path('state', 'autoupdate.json'), state)
        result = autoupdate.run()
        self.assertEqual(result, {'status': 'current', 'refreshed': True})
        self.assertEqual(autoupdate.notice(), '')
        self.assertNotIn('pending_refresh', store.read_json(paths.path('state', 'autoupdate.json')))

    def test_explicit_check_continues_past_pending_refresh_to_latest_release(self):
        self.release()
        (self.root / 'fail-refresh').touch()
        self.assertEqual(autoupdate.run()['status'], 'skipped')
        (self.root / 'fail-refresh').unlink()
        (self.origin / '.codex-plugin' / 'plugin.json').write_text('{"name":"honey","version":"1.2.0"}', encoding="utf-8")
        (self.origin / 'honey' / '__init__.py').write_text('__version__ = "1.2.0"\n', encoding="utf-8")
        self.git(self.origin, 'add', '.codex-plugin/plugin.json', 'honey/__init__.py')
        self.git(self.origin, 'commit', '-qm', 'third release')
        self.git(self.origin, 'tag', 'v1.2.0')
        result = autoupdate.run(force=True)
        self.assertEqual(result.get('to'), '1.2.0', result)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), self.git(self.origin, 'rev-parse', 'HEAD'))
        self.assertEqual((self.cache.parent / '1.2.0' / 'installed-version').read_text(encoding="utf-8"), '1.2.0')

    def test_feature_branch_is_not_reported_ready_when_release_is_current(self):
        self.git(self.checkout, 'switch', '-c', 'feature/local-work')
        result = autoupdate.run()
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('not on main', result['reason'])

    def test_local_main_commits_are_not_reported_ready(self):
        (self.checkout / 'local.txt').write_text('local work', encoding="utf-8")
        self.git(self.checkout, 'add', 'local.txt')
        self.git(self.checkout, 'commit', '-qm', 'local work')
        result = autoupdate.run()
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('own commits', result['reason'])

    def test_failed_refresh_is_retried_without_a_false_success(self):
        new_head = self.release()
        (self.root / "fail-refresh").touch()
        result = autoupdate.run()
        self.assertEqual(result.get("status"), "skipped", result)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), new_head)
        self.assertEqual(autoupdate.notice(), "")
        self.assertEqual(store.read_json(paths.path("synced.json"))["head"], self.old_head)
        self.assertGreater(store.read_json(paths.path("state", "autoupdate.json"))["checked"], 0)
        (self.root / "fail-refresh").unlink()
        state = store.read_json(paths.path("state", "autoupdate.json"))
        state["checked"] = 0
        store.write_json(paths.path("state", "autoupdate.json"), state)
        result = autoupdate.run()
        self.assertEqual(result.get("status"), "updated", result)
        self.assertEqual((self.cache.parent / "1.1.0" / "installed-version").read_text(encoding="utf-8"), "1.1.0")
        self.assertEqual(store.read_json(paths.path("synced.json"))["head"], new_head)

    def test_pending_refresh_does_not_install_later_local_edits(self):
        self.release()
        (self.root / "fail-refresh").touch()
        self.assertEqual(autoupdate.run().get("status"), "skipped")
        (self.root / "fail-refresh").unlink()
        (self.checkout / "honey" / "__init__.py").write_text("# local edit\n", encoding="utf-8")
        state = store.read_json(paths.path("state", "autoupdate.json"))
        state["checked"] = 0
        store.write_json(paths.path("state", "autoupdate.json"), state)
        result = autoupdate.run()
        self.assertEqual(result.get("status"), "skipped", result)
        self.assertIn("local changes", result.get("reason", ""))
        self.assertFalse((self.cache.parent / "1.1.0").exists())
        self.assertIn("pending_refresh", store.read_json(paths.path("state", "autoupdate.json")))

    def test_local_marketplace_works_without_a_previous_sync(self):
        Path(paths.path("synced.json")).unlink()
        new_head = self.release()
        result = autoupdate.run()
        self.assertEqual(result.get("status"), "updated", result)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), new_head)
        self.assertEqual(store.read_json(paths.path("synced.json"))["head"], new_head)

    def test_session_start_triggers_update_and_next_session_sees_notice(self):
        self.release()
        jobs = []

        def spawn(args):
            jobs.append(args)
            if args == ["autoupdate"]:
                self.assertEqual(autoupdate.run().get("status"), "updated")

        with patch.object(dispatch.observe, "spawn", side_effect=spawn):
            first = dispatch.Ctx("codex", {"session_id": "first", "source": "startup", "cwd": str(self.root)})
            dispatch.session_start(first)
            self.assertIn(["autoupdate"], jobs)
            second = dispatch.Ctx("codex", {"session_id": "second", "source": "startup", "cwd": str(self.root)})
            message = dispatch.session_start(second)
        self.assertIn("auto-updated 1.0.0 → 1.1.0", json.dumps(message, ensure_ascii=False))
        self.assertEqual(jobs.count(["autoupdate"]), 1)

    def test_dirty_checkout_is_not_updated_or_refreshed(self):
        self.release()
        (self.checkout / "honey" / "__init__.py").write_text("# local edit\n", encoding="utf-8")
        result = autoupdate.run()
        self.assertEqual(result.get("status"), "skipped", result)
        self.assertIn("local changes", result.get("reason", ""))
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.old_head)
        self.assertFalse((self.cache.parent / "1.1.0").exists())

    def test_failed_remote_plugin_refresh_does_not_report_current(self):
        (self.root / "codex" / "config.toml").write_text('[marketplaces.honey-stack]\nsource_type = "git"\n', encoding="utf-8")
        # A Claude executable alone must not cause an attempt to install its plugin.
        with patch.object(install, "_cli", return_value=str(self.codex)):
            result = autoupdate.run()
        self.assertEqual(result.get("status"), "skipped", result)
        self.assertIn("refresh failed", result.get("reason", ""))
        self.assertNotIn("claude", result.get("reason", ""))

    def test_disabled_codex_plugin_is_not_reenabled(self):
        config = self.root / "codex" / "config.toml"
        config.write_text('[marketplaces.honey-stack]\nsource_type = "local"\n'
                          '[plugins."honey@honey-stack"]\nenabled = false\n', encoding="utf-8")
        self.release()
        result = autoupdate.run()
        self.assertEqual(result.get("status"), "skipped", result)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.old_head)
        self.assertFalse((self.cache.parent / "1.1.0").exists())

    def test_different_local_marketplaces_do_not_update_either_checkout(self):
        other = self.root / "other-checkout"
        subprocess.run([*GIT, "clone", "-q", str(self.origin), str(other)], check=True)
        claude = self.root / "claude"
        (claude / "plugins").mkdir(parents=True)
        (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"honey@honey-stack": True}}), encoding="utf-8")
        (claude / "plugins" / "known_marketplaces.json").write_text(json.dumps(
            {"honey-stack": {"source": {"source": "directory", "path": str(other)}}}))
        self.release()
        with patch.object(install, "_cli", return_value=str(self.codex)):
            result = autoupdate.run()
        self.assertEqual(result.get("status"), "skipped", result)
        self.assertIn("different local checkouts", result.get("reason", ""))
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.old_head)
        self.assertEqual(self.git(other, "rev-parse", "HEAD"), self.old_head)


if __name__ == "__main__":
    unittest.main()
