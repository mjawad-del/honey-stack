"""Direct test imports must not touch the user's real RSI home or call paid providers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from helpers import REPO


class HarnessIsolation(unittest.TestCase):
    def test_direct_import_uses_throwaway_state_and_fake_provider(self):
        env = dict(os.environ, PYTHONPATH=str(Path(REPO, 'tests')))
        for key in ('HONEY_HOME', 'HONEY_FAKE_LLM', 'HONEY_NO_AUTOUPDATE'):
            env.pop(key, None)
        code = "import helpers,os,json; print(json.dumps([os.environ['HONEY_HOME'],os.path.isfile(os.environ['HONEY_FAKE_LLM']),os.environ['HONEY_NO_AUTOUPDATE']]))"
        result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, encoding='utf-8', check=True)
        path, fake, disabled = json.loads(result.stdout)
        self.assertNotEqual(Path(path), Path.home() / '.honey')
        self.assertTrue(fake)
        self.assertEqual(disabled, '1')
        self.assertFalse(Path(path).exists(), 'temporary test state should be cleaned up')


if __name__ == '__main__':
    unittest.main()
