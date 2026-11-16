"""Tool-call hooks run ~180 times a session, so they must not load per-event or heavy modules.

Checks which modules a warm PreToolUse/PostToolUse hook imports (deterministic, unlike a timing test).
Measured on an M1 Pro: dropping these took a tool-call hook from ~55 ms to ~31 ms.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from helpers import REPO, hook

PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from honey import dispatch
try:
    dispatch.run("claude")
finally:
    sys.stderr.write(json.dumps(sorted(sys.modules)))
"""
HEAVY = {"honey.autoupdate", "honey.intent", "honey.receipt", "honey.rsi.curate", "honey.rsi.review",
         "honey.rsi.signals", "random", "tempfile", "shutil", "_hashlib", "argparse", "traceback", "glob"}

home = tempfile.mkdtemp(prefix="honey-imports-")
try:
    os.environ["HONEY_HOME"] = home
    base = {"session_id": "imports", "cwd": REPO}
    hook("claude", dict(base, hook_event_name="SessionStart", source="startup"))  # warm session state
    events = {
        "pre": dict(base, hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "git status"}, tool_use_id="a"),
        "post": dict(base, hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "git status"},
                     tool_use_id="a", tool_response={"stdout": "ok", "exit_code": 0}),
        "edit": dict(base, hook_event_name="PostToolUse", tool_name="Edit",
                     tool_input={"file_path": os.path.join(REPO, "x.py"), "new_string": "x = 1"}, tool_use_id="b"),
    }
    for name, payload in events.items():
        r = subprocess.run([sys.executable, "-c", PROBE, REPO], input=json.dumps(payload), capture_output=True,
                           text=True, env=dict(os.environ))
        loaded = set(json.loads(r.stderr[r.stderr.index("["):]))
        assert not loaded & HEAVY, (name, sorted(loaded & HEAVY))
        assert not os.path.exists(os.path.join(home, "logs", "hook-errors.log")), open(os.path.join(home, "logs", "hook-errors.log")).read()
finally:
    # SessionStart starts detached jobs that may still hold logs/jobs.log open; Windows can't delete open files.
    shutil.rmtree(home, ignore_errors=True)
print("hook imports ok")
