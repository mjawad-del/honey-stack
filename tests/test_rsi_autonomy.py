"""Autonomous RSI through its CLI and real persistent proposal effects."""
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch as mock

from helpers import REPO
from honey import paths
from honey.rsi import autonomy, curate, guards, improve, review, store

agent_home = tempfile.mkdtemp()
os.environ["CLAUDE_CONFIG_DIR"] = agent_home
doc = os.path.join(agent_home, "CLAUDE.md")
store.write_text(doc, "# personal\n")


def cli(*args):
    return subprocess.run([sys.executable, os.path.join(REPO, "bin", "honey"), "rsi", *args],
                          capture_output=True, text=True, encoding="utf-8", env=dict(os.environ))


def proposal(key, line="- WHEN testing → prove it"):
    p = review.add(1, "instr_line", key, "repeated correction", "add line", [], key)
    p["edit"] = {"file": "claude", "action": "add", "line": line}
    store.write_json(paths.path("proposals", p["id"] + ".json"), p)
    return p


p = proposal("autonomous instruction")
off = cli("catchup")
assert off.returncode == 0, off.stderr
assert open(doc, encoding="utf-8").read() == "# personal\n"
on = cli("autonomy", "on")
assert on.returncode == 0, on.stderr
assert "enabled" in on.stdout, on.stdout
run = cli("catchup")
assert run.returncode == 0, run.stderr
assert "WHEN testing → prove it" in open(doc, encoding="utf-8").read()
saved = store.read_json(paths.path("proposals", p["id"] + ".json"))
assert saved["status"] == "applied" and saved["decision_source"] == "automatic", saved
assert not os.path.exists(paths.path("golden.jsonl"))
before = open(doc, encoding="utf-8").read()
assert cli("catchup").returncode == 0
assert open(doc, encoding="utf-8").read() == before
assert "enabled" in cli("autonomy", "status").stdout
assert cli("autonomy", "off").returncode == 0
p2 = proposal("disabled instruction", "- disabled")
assert cli("catchup").returncode == 0
assert "- disabled" not in open(doc, encoding="utf-8").read()
assert store.read_json(paths.path("proposals", p2["id"] + ".json"))["status"] == "pending"

# Audit-generated toolkit repairs have executable, scoped payloads and backups.
settings = os.path.join(agent_home, "settings.json")
store.write_json(settings, {"keep": 42, "hooks": {"UserPromptSubmit": [
    {"matcher": "", "hooks": [{"type": "command", "command": "python /old/mct-prompt-router.py"},
                                {"type": "command", "command": "my-other-hook"}]}]}})
repairs = review.audit()
assert len(repairs) == 1 and repairs[0].get("repair"), repairs
assert cli("autonomy", "on").returncode == 0
assert cli("catchup").returncode == 0
result = store.read_json(settings)
assert result["keep"] == 42
assert result["hooks"]["UserPromptSubmit"][0]["hooks"] == [{"type": "command", "command": "my-other-hook"}], result
saved = store.read_json(paths.path("proposals", repairs[0]["id"] + ".json"))
assert saved["status"] == "applied" and saved.get("repair_actions"), saved
assert os.path.exists(saved["repair_actions"][0]["backup"])

# Verified guard proposals activate automatically; unstructured diagnoses do not.
b = curate.new_bullet("committing", "ask first", "global", "preference", "observed", 3, [], "active")
curate.save([b])
g = {"id": "g-test", "bullet": b["id"], "regex": r"^git commit", "unless_prompt": "commit",
     "message": "ask before commit", "action": "block", "status": "proposed",
     "verify": {"replay_hits": 1, "replay_n": 1, "fp": 0, "fp_n": 10}}
guards.save([g])
gp = review.add(3, "guard", "guard", "violated", "activate", [], "autonomous guard")
gp["guard"] = g["id"]
store.write_json(paths.path("proposals", gp["id"] + ".json"), gp)
bad = review.add(1, "toolkit_fix", "diagnose", "unknown issue", "Investigate; do not execute this prose", [], "unknown toolkit issue")
good = proposal("after bad diagnosis", "- still apply independent improvements")
assert cli("catchup").returncode == 0
assert guards.load()[0]["status"] == "active"
assert "still apply independent improvements" in open(doc, encoding="utf-8").read()
blocked = store.read_json(paths.path("proposals", bad["id"] + ".json"))
assert blocked["status"] == "pending" and "structured" in blocked["last_error"], blocked
assert not os.path.exists(paths.path("golden.jsonl"))

# Patch dispatch uses the eval-gated accept path; upstream remains incomplete
# until its linked child has actually been accepted in a later bounded pass.
up = review.add(2, "upstream_port", "port", "friction", "draft", [], "upstream parent")
up.update(skill="debug", upstream="verify the reproduction")
store.write_json(paths.path("proposals", up["id"] + ".json"), up)
child = review.add(2, "skill_patch", "patch", "friction", "apply", [], "patch child")
review.decide(child["id"], "skip")


def draft(*args):
    child["status"] = "pending"
    store.write_json(paths.path("proposals", child["id"] + ".json"), child)
    return {"proposed": child["id"]}


with mock("honey.rsi.patch.propose", side_effect=draft) as propose, mock("honey.rsi.patch.accept", return_value="synced") as accept:
    with store.lock():
        improve.run()
    parent = store.read_json(paths.path("proposals", up["id"] + ".json"))
    assert parent.get("child") == child["id"] and parent["status"] == "pending", parent
    assert not accept.called
    with store.lock():
        autonomy.run()
    assert accept.call_count == 1
    with store.lock():
        autonomy.run()
    assert store.read_json(paths.path("proposals", up["id"] + ".json"))["status"] == "applied"
    assert propose.call_count == 1
print("autonomy ok")
