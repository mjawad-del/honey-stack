import json
import os
import subprocess
import sys
import atexit
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Individual scripts and accidental discovery must never fall back to real RSI data.
if not os.environ.get('HONEY_HOME'):
    _isolated_home = tempfile.TemporaryDirectory(prefix='honey-test-')
    atexit.register(_isolated_home.cleanup)
    os.environ['HONEY_HOME'] = _isolated_home.name
    _fake = os.path.join(_isolated_home.name, 'none-reflection.json')
    with open(_fake, 'w', encoding="utf-8") as f:
        json.dump({'session_summary': '', 'candidates': [], 'bullet_labels': [], 'skill_feedback': [],
                   'toolkit_misfires': [], 'progress': [], 'project_facts_dropped': 0, 'none_reason': 'test'}, f)
    os.environ['HONEY_FAKE_LLM'] = _fake
    os.environ['HONEY_NO_AUTOUPDATE'] = '1'


def hook(agent, payload):
    """Run the real hook entrypoint the way the agents do; return parsed stdout (or {})."""
    r = subprocess.run([sys.executable, os.path.join(REPO, "bin", "honey"), "hook", "--agent", agent],
                       input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", env=dict(os.environ))
    assert r.returncode == 0, r.stderr
    errs = os.path.join(os.environ["HONEY_HOME"], "logs", "hook-errors.log")
    assert not os.path.exists(errs), open(errs, encoding="utf-8").read()
    return json.loads(r.stdout) if r.stdout.strip() else {}


def ctx_text(out):
    return (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
