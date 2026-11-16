"""Doctor consults the native hook registry instead of counting stale approvals."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import REPO
from honey import codex_hooks, doctor, paths


@unittest.skipIf(os.name == 'nt', 'fake CLI fixture is an extensionless script; Windows only runs .exe/.cmd')
class CodexHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, HONEY_HOME=str(root / 'honey'),
                              CODEX_HOME=str(root / 'codex-home'),
                              PATH=str(root) + os.pathsep + os.environ.get('PATH', ''))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.log = root / 'requests.jsonl'
        self.executable = root / 'codex'
        self.executable.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys, time
for line in sys.stdin:
    row = json.loads(line)
    if 'id' not in row:
        continue
    with open(os.environ['REGISTRY_LOG'], 'a', encoding="utf-8") as f:
        f.write(json.dumps(row['method']) + '\\n')
    if row['method'] == 'initialize':
        result = {}
    elif row['method'] == 'hooks/list':
        time.sleep(float(os.environ.get('REGISTRY_DELAY', '0')))
        result = json.loads(os.environ['REGISTRY_RESULT'])
    else:
        raise RuntimeError('unexpected mutation: ' + row['method'])
    print(json.dumps({'id': row['id'], 'result': result}), flush=True)
''')
        self.executable.chmod(0o700)
        os.environ['REGISTRY_LOG'] = str(self.log)
        self.response('modified')

    def response(self, trust, **overrides):
        hooks = [dict(key='honey@honey-stack:hooks/codex-hooks.json:' + event + ':0:0',
                      pluginId='honey@honey-stack', enabled=True, trustStatus=trust,
                      eventName=event, **overrides) for event in sorted(codex_hooks.EXPECTED_EVENTS)]
        os.environ['REGISTRY_RESULT'] = json.dumps({'data': [
            {'cwd': os.getcwd(), 'hooks': hooks, 'warnings': [], 'errors': []}]})

    def test_doctor_reports_modified_approval_instead_of_trusted(self):
        config = Path(paths.codex_dir(), 'config.toml')
        config.parent.mkdir(parents=True)
        config.write_text('[hooks.state."honey@honey-stack:hooks/codex-hooks.json:stop:0:0"]\n'
                          'trusted_hash = "old-hash"\n', encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.main(['--agent', 'codex', '--session', 'absent', '--json'])
        result = json.loads(out.getvalue())
        self.assertEqual(result['hook_trust']['status'], 'review_required')
        self.assertEqual(result['hook_trust']['modified'], 9)
        self.assertEqual(code, 1)
        self.assertEqual([json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines()],
                         ['initialize', 'hooks/list'])
        self.assertFalse(Path(paths.home()).exists(), 'diagnostics must not write RSI state')

    def test_trusted_registry_is_reported_without_changing_approvals(self):
        self.response('trusted')
        result = codex_hooks.inspect()
        self.assertEqual(result['status'], 'trusted')
        self.assertEqual(result['trusted'], 9)
        self.assertFalse(Path(paths.codex_dir(), 'config.toml').exists())

    def test_disabled_unknown_and_missing_are_not_trusted(self):
        self.response('trusted')
        payload = json.loads(os.environ['REGISTRY_RESULT'])
        payload['data'][0]['hooks'][0]['enabled'] = False
        os.environ['REGISTRY_RESULT'] = json.dumps(payload)
        self.assertEqual(codex_hooks.inspect()['status'], 'disabled')
        self.response('new-unrecognized-status')
        self.assertEqual(codex_hooks.inspect()['status'], 'unknown')
        os.environ['REGISTRY_RESULT'] = json.dumps({'data': [{'hooks': [], 'errors': []}]})
        self.assertEqual(codex_hooks.inspect()['status'], 'missing')

    def test_malformed_registry_and_timeout_are_visible(self):
        os.environ['REGISTRY_RESULT'] = json.dumps({'data': None})
        self.assertEqual(codex_hooks.inspect()['status'], 'unknown')
        self.response('trusted')
        os.environ['REGISTRY_DELAY'] = '1'
        result = codex_hooks.inspect(timeout=0.1)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('timed out', result['reason'])

    def test_trusted_but_incomplete_registry_cannot_claim_ready(self):
        self.response('trusted')
        payload = json.loads(os.environ['REGISTRY_RESULT'])
        payload['data'][0]['hooks'] = [h for h in payload['data'][0]['hooks'] if h['eventName'] == 'stop']
        os.environ['REGISTRY_RESULT'] = json.dumps(payload)
        result = codex_hooks.inspect()
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('sessionStart', result['missing_events'])
        self.assertIn('sessionEnd', result['missing_events'])


if __name__ == '__main__':
    unittest.main()
