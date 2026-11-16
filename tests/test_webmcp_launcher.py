"""WebMCP's browser owner must start promptly, clean up promptly and preserve runner failures."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


NODE = shutil.which("node")
ASSETS = Path(__file__).resolve().parents[1] / "skills/qa/assets/webmcp"
LAUNCHER = ASSETS / "run.mjs"
HELPER = ASSETS / "webmcp.ts"
COMPILE_CACHE = bool(NODE) and subprocess.run(
    [NODE, "-e", "process.exit(require('node:module').enableCompileCache ? 0 : 1)"]).returncode == 0
# Node 23.6+ strips TypeScript types itself, so the shipped helper can be imported as is.
STRIPS_TYPES = bool(NODE) and subprocess.run(
    [NODE, "--input-type=module", "-e", f"await import({json.dumps(str(HELPER))})"],
    capture_output=True).returncode == 0
CORES = int(subprocess.run([NODE, "-p", "require('node:os').availableParallelism()"],
                           capture_output=True, text=True, encoding="utf-8").stdout) if NODE else 1

BROWSER = """#!/usr/bin/env node
import { chmodSync, existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
const profile = process.argv.find(a => a.startsWith('--user-data-dir=')).split('=').slice(1).join('=');
const audit = { pid: process.pid, profile, argv: process.argv.slice(1) };
const save = () => writeFileSync(join(process.env.HONEY_TEST_AUDIT, `${process.pid}.json`), JSON.stringify(audit));
save();
if (process.env.HONEY_TEST_BROWSER_FAIL === '1') {
  if (process.env.HONEY_TEST_WAIT_RUNNER === '1') {
    while (!existsSync(join(process.env.HONEY_TEST_AUDIT, 'runner-started'))) {
      await new Promise(resolve => setTimeout(resolve, 5));
    }
  }
  process.exit(3);
}
if (process.env.HONEY_TEST_LOCK === '1') {
  mkdirSync(join(profile, 'locked'));
  writeFileSync(join(profile, 'locked', 'file'), '');
  chmodSync(join(profile, 'locked'), 0o500);
}
process.on('SIGTERM', () => {
  audit.terminatedAt = Date.now();
  save();
  if (process.env.HONEY_TEST_IGNORE_TERM !== '1') process.exit(0);
});
process.on('SIGINT', () => {});
setTimeout(() => writeFileSync(join(profile, 'DevToolsActivePort'), '43210\\n'),
  Number(process.env.HONEY_TEST_BROWSER_DELAY ?? 0));
setInterval(() => {}, 10000);
"""

# Stands in for the e2e CLI: waits for every browser's port file, as the config's provider does.
E2E = """
const { existsSync, readFileSync, writeFileSync } = require('node:fs');
const { delimiter, join } = require('node:path');
const files = (process.env.HONEY_WEBMCP_PORT_FILES ?? '').split(delimiter).filter(Boolean);
const record = { files: files.length, readyAtStart: files.filter(f => existsSync(f)).length,
  compileCache: process.env.NODE_COMPILE_CACHE ?? null, terms: 0 };
const save = () => writeFileSync(join(process.env.HONEY_TEST_AUDIT, 'e2e.json'), JSON.stringify(record));
process.on('SIGINT', () => {});
process.on('SIGTERM', () => {
  record.terms += 1; record.terminated = true; save();
  setTimeout(() => process.exit(130), Number(process.env.HONEY_TEST_TERM_DELAY ?? 0));
});
writeFileSync(join(process.env.HONEY_TEST_AUDIT, 'runner-started'), '');
const deadline = Date.now() + 5000;
const poll = () => {
  if (files.length && files.every(f => existsSync(f) && readFileSync(f, 'utf8').startsWith('43210'))) {
    save();
    if (process.env.HONEY_TEST_HOLD !== '1') process.exit(Number(process.env.HONEY_TEST_EXIT));
    writeFileSync(join(process.env.HONEY_TEST_AUDIT, 'ready'), '');
    setInterval(() => {}, 10000);
    return;
  }
  if (Date.now() > deadline) { record.timedOut = true; save(); process.exit(99); }
  setTimeout(poll, 10);
};
poll();
"""


@unittest.skipUnless(NODE, "WebMCP launcher needs Node.js")
@unittest.skipIf(os.name == 'nt', 'launcher cleanup tests rely on POSIX signals and process groups')
class WebMCPLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Runs before the directory cleanup, so a locked test profile stays deletable.
        self.addCleanup(lambda: [os.chmod(p, 0o700) for p in self.root.rglob("*") if p.is_dir()])
        self.audit = self.root / "audit"
        self.audit.mkdir()
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.browser = self.root / "browser.mjs"
        self.browser.write_text(BROWSER, encoding="utf-8")
        self.browser.chmod(0o700)
        modules = self.root / "node_modules"
        self.playwright = modules / "playwright-core"
        self.playwright.mkdir(parents=True)
        (self.playwright / "index.js").write_text(
            "exports.chromium = { executablePath: () => process.env.HONEY_TEST_BROWSER };\n"
        , encoding="utf-8")
        engine = modules / "@e2e-dev/web"
        engine.mkdir(parents=True)
        (engine / "package.json").write_text(json.dumps({"name": "@e2e-dev/web", "main": "index.js"}), encoding="utf-8")
        (engine / "index.js").write_text("", encoding="utf-8")
        e2e = modules / "e2e"
        e2e.mkdir()
        (e2e / "package.json").write_text(json.dumps({"name": "e2e", "main": "cli.cjs", "bin": {"e2e": "cli.cjs"}}), encoding="utf-8")
        (e2e / "cli.cjs").write_text(E2E, encoding="utf-8")
        self.env = {
            **os.environ,
            "PATH": str(Path(NODE).parent) + os.pathsep + os.environ.get("PATH", ""),
            "HONEY_TEST_BROWSER": str(self.browser),
            "HONEY_TEST_AUDIT": str(self.audit),
            "TMPDIR": str(self.tmp),
        }
        for name in ("HONEY_WEBMCP_BROWSERS", "HONEY_WEBMCP_HEADLESS", "NODE_COMPILE_CACHE",
                     "NODE_DISABLE_COMPILE_CACHE", "CI"):
            self.env.pop(name, None)

    def clear_audit(self):
        for path in self.audit.iterdir():
            path.unlink()

    def assert_cleaned_up(self):
        browsers = [json.loads(p.read_text(encoding="utf-8")) for p in self.audit.glob("[0-9]*.json")]
        self.assertTrue(browsers, "No browser was started")
        for browser in browsers:
            with self.assertRaises(ProcessLookupError):
                os.kill(browser["pid"], 0)
        self.assertEqual(list(self.tmp.glob("honey-webmcp-*")), [], "Owned profiles were not removed")
        return browsers

    def run_launcher(self, *args, expect=0, audit_optional=False, **env):
        started = time.monotonic()
        result = subprocess.run(
            [NODE, str(LAUNCHER), "run", *args], cwd=self.root,
            env={**self.env, "HONEY_TEST_EXIT": str(expect or 0), **env},
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        finished_at = time.time() * 1000
        if expect is None:
            self.assertNotEqual(result.returncode, 0, result.stderr)
        else:
            self.assertEqual(result.returncode, expect, result.stderr)
        browsers = self.assert_cleaned_up()
        audit = self.audit / "e2e.json"
        runner = None if audit_optional and not audit.exists() else json.loads(audit.read_text(encoding="utf-8"))
        return browsers, runner, result, finished_at, time.monotonic() - started

    def test_completed_browser_does_not_wait_for_unused_shutdown_deadline(self):
        (browser,), _, _, finished_at, _ = self.run_launcher("--workers", "1")
        self.assertLess(finished_at - browser["terminatedAt"], 1000,
                        "An unused 2s cleanup timer kept the launcher alive")

    def test_test_failure_is_preserved_and_still_cleans_up_promptly(self):
        (browser,), _, _, finished_at, _ = self.run_launcher("--workers", "1", expect=1)
        self.assertLess(finished_at - browser["terminatedAt"], 1000)

    def test_unresponsive_browser_is_killed_at_the_shutdown_deadline(self):
        (browser,), _, _, finished_at, elapsed = self.run_launcher(
            "--workers", "1", HONEY_TEST_IGNORE_TERM="1")
        self.assertGreaterEqual(finished_at - browser["terminatedAt"], 1800)
        self.assertLess(elapsed, 6)

    def test_e2e_starts_while_the_browser_boots(self):
        _, runner, _, _, _ = self.run_launcher("--workers", "1", HONEY_TEST_BROWSER_DELAY="500")
        self.assertEqual(runner["readyAtStart"], 0, "e2e waited for the browser before starting")
        if COMPILE_CACHE:
            self.assertTrue(runner["compileCache"], "e2e did not inherit Node's compile cache")

    def test_one_isolated_browser_per_worker(self):
        browsers, runner, _, _, _ = self.run_launcher("--workers", "3")
        self.assertEqual((len(browsers), runner["files"]), (3, 3))
        self.assertEqual(len({b["profile"] for b in browsers}), 3, "Workers shared a profile")
        self.clear_audit()
        browsers, runner, _, _, _ = self.run_launcher("--workers=2", HONEY_WEBMCP_BROWSERS="1")
        self.assertEqual((len(browsers), runner["files"]), (1, 1),
                         "HONEY_WEBMCP_BROWSERS did not override --workers")

    def test_browser_count_follows_e2e_worker_default(self):
        browsers, _, _, _, _ = self.run_launcher()
        self.assertEqual(len(browsers), max(1, CORES // 2))
        self.clear_audit()
        browsers, _, _, _, _ = self.run_launcher(CI="1")
        self.assertEqual(len(browsers), 1)

    def test_browser_startup_failure_stops_the_run(self):
        _, runner, result, _, elapsed = self.run_launcher(
            "--workers", "1", expect=None, HONEY_TEST_BROWSER_FAIL="1", HONEY_TEST_WAIT_RUNNER="1")
        self.assertTrue(runner.get("terminated"), "e2e kept waiting for a browser that had exited")
        self.assertLess(elapsed, 4)
        self.assertIn("WebMCP browser stopped", result.stderr)

    def test_browser_failure_before_runner_audit_still_cleans_up(self):
        # Model a runner terminated before it can initialize its audit writer.
        (self.root / "node_modules/e2e/cli.cjs").write_text(
            "process.on('SIGTERM', () => process.exit(143)); setInterval(() => {}, 10000);\n")
        _, runner, result, _, elapsed = self.run_launcher(
            "--workers", "1", expect=None, audit_optional=True, HONEY_TEST_BROWSER_FAIL="1")
        self.assertIsNone(runner)
        self.assertLess(elapsed, 4)
        self.assertIn("WebMCP browser stopped", result.stderr)

    def test_repeated_interrupts_still_clean_up(self):
        process = subprocess.Popen(
            [NODE, str(LAUNCHER), "run", "--workers", "2"], cwd=self.root,
            env={**self.env, "HONEY_TEST_HOLD": "1", "HONEY_TEST_TERM_DELAY": "1000"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        deadline = time.monotonic() + 5
        while not (self.audit / "ready").exists():
            self.assertLess(time.monotonic(), deadline, "e2e never saw its browsers")
            time.sleep(0.02)
        process.send_signal(signal.SIGINT)
        time.sleep(0.1)
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 1, stderr)
        self.assert_cleaned_up()
        self.assertEqual(json.loads((self.audit / "e2e.json").read_text(encoding="utf-8"))["terms"], 2,
                         "A repeated interrupt was not forwarded to e2e")

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "Needs an undeletable directory")
    def test_profile_removal_failure_keeps_the_e2e_exit_code(self):
        result = subprocess.run(
            [NODE, str(LAUNCHER), "run", "--workers", "1"], cwd=self.root,
            env={**self.env, "HONEY_TEST_EXIT": "0", "HONEY_TEST_LOCK": "1"},
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not remove", result.stderr)

    def test_prefers_playwright_headless_shell(self):
        shell = self.root / "shell.mjs"
        shutil.copy(self.browser, shell)
        shell.chmod(0o700)
        (self.playwright / "lib").mkdir()
        (self.playwright / "lib/coreBundle.js").write_text(
            "exports.registry = { registry: { findExecutable: () => "
            "({ executablePath: () => process.env.HONEY_TEST_SHELL }) } };\n"
        )
        (browser,), _, _, _, _ = self.run_launcher("--workers", "1", HONEY_TEST_SHELL=str(shell))
        self.assertEqual(Path(browser["argv"][0]).name, "shell.mjs")
        self.assertIn("--headless", browser["argv"])
        self.assertIn("--enable-features=WebMCP,CDPScreenshotNewSurface", browser["argv"])
        self.assertIn("--remote-debugging-address=127.0.0.1", browser["argv"])
        self.clear_audit()
        (browser,), _, _, _, _ = self.run_launcher(
            "--workers", "1", HONEY_TEST_SHELL=str(shell), HONEY_WEBMCP_HEADLESS="new")
        self.assertEqual(Path(browser["argv"][0]).name, "browser.mjs")
        self.assertIn("--headless=new", browser["argv"])


@unittest.skipUnless(STRIPS_TYPES, "Importing the TypeScript helper needs Node 23.6+")
class WebMCPProviderTests(unittest.TestCase):
    def provider(self, script, **env):
        result = subprocess.run(
            [NODE, "--input-type=module", "-e",
             f"const {{ webmcpBrowsers: p }} = await import({json.dumps(str(HELPER))});\n{script}"],
            env={**os.environ, **env}, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_leases_spread_over_browsers_and_wait_for_their_ports(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "a", Path(temp) / "b"
            first.write_text("41001\n", encoding="utf-8")
            out = self.provider("""
const { writeFileSync } = await import('node:fs');
setTimeout(() => writeFileSync(process.env.SECOND, '41002\\n'), 200);
const logs = [];
const request = { env: process.env, signal: new AbortController().signal, log: line => logs.push(line) };
const leases = [];
for (let i = 0; i < 3; i++) leases.push(await p.acquire(request));
console.log(JSON.stringify({ leases, logs }));
""", HONEY_WEBMCP_PORT_FILES=os.pathsep.join([str(first), str(second)]), SECOND=str(second))
        self.assertEqual([lease["cdpEndpoint"] for lease in out["leases"]],
                         ["http://127.0.0.1:41001", "http://127.0.0.1:41002", "http://127.0.0.1:41001"])
        self.assertEqual(len({lease["id"] for lease in out["leases"]}), 3)
        self.assertEqual(len(out["logs"]), 1, "Sharing a browser was not reported exactly once")

    def test_waiting_for_a_port_can_be_aborted(self):
        with tempfile.TemporaryDirectory() as temp:
            out = self.provider("""
const controller = new AbortController();
setTimeout(() => controller.abort(), 100);
const started = Date.now();
const error = await p.acquire({ env: process.env, signal: controller.signal, log() {} }).catch(e => e);
console.log(JSON.stringify({ name: error.name, ms: Date.now() - started }));
""", HONEY_WEBMCP_PORT_FILES=str(Path(temp) / "missing"))
        self.assertEqual(out["name"], "AbortError")
        self.assertLess(out["ms"], 1000)

    def test_refuses_to_run_without_the_launcher(self):
        out = self.provider("""
const error = await p.acquire({ env: {}, signal: new AbortController().signal, log() {} }).catch(e => e);
console.log(JSON.stringify({ message: error.message }));
""", HONEY_WEBMCP_PORT_FILES="")
        self.assertIn("scripts/run-webmcp.mjs", out["message"])


if __name__ == "__main__":
    unittest.main()
