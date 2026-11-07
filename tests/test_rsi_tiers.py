"""Coach, Tier 1 instruction edits, Tier 3 learned guards, Tier 2 hunk rules and the radar — all with a canned LLM."""
import json
import os
import subprocess
import sys
import tempfile

from helpers import REPO, ctx_text, hook

H = os.environ["HONEY_HOME"]
sys.path.insert(0, REPO)
from honey.rsi import coach, curate, guards, instructions, patch, radar, review, store, text

FAKE = os.path.join(tempfile.mkdtemp(), "fake.json")
os.environ["HONEY_FAKE_LLM"] = FAKE


def fake(obj):
    json.dump(obj, open(FAKE, "w"))


# --- coach: insufficient data, then real numbers
assert "Insufficient data" in coach.report()
prof = {"weekly": {f"{coach._weeks(1)[0]}:claude": {"sessions": 6, "user_turns": 40, "correction": 8, "still": 2, "frustration": 1,
                                                     "accept": 4, "continue": 3, "false_done": 2, "unneeded_q": 4, "tool_fail": 3, "compact": 1, "notes": 1}},
        "domains": {"git-delivery": {"repeating": 3, "growing": 0}}, "baseline": {"slash_distinct_days": [["/model", 46], ["/effort", 41], ["/usage", 35]]}}
store.write_json(os.path.join(H, "profile.json"), prof)
store.write_json(os.path.join(H, "scorecards.json"), {"ship": {"inv_model": 5, "neg": 3, "pos": 1}})
r = coach.report()
assert "corrections: 2.0 per 10 turns" in r and "git-delivery" in r and "patch-candidate" in r and "/usage" in r, r

# --- Tier 1: graduation → apply into managed block → revert
cdir = tempfile.mkdtemp()
os.environ["CLAUDE_CONFIG_DIR"] = cdir
open(os.path.join(cdir, "CLAUDE.md"), "w").write("# mine\n- keep this\n")
b = curate.new_bullet("pushing", "push only, no PR unless asked", "global", "preference", "observed", 2, [{"sid": "x", "q": "q"}], "active")
b.update(helpful=4, activated="2026-01-01T00:00:00Z")
curate.save([b])
ps = instructions.graduate()
assert ps and ps[0]["type"] == "instr_line"
subprocess.run([sys.executable, os.path.join(REPO, "bin", "honey"), "rsi", "decide", ps[0]["id"], "apply"], check=True, capture_output=True, env=dict(os.environ))
doc = open(os.path.join(cdir, "CLAUDE.md")).read()
assert instructions.BEGIN in doc and "push only, no PR unless asked" in doc and "- keep this" in doc, doc
assert curate.load()[0]["status"] == "archived"   # graduated out of the brief
instructions.revert(ps[0]["id"])
assert open(os.path.join(cdir, "CLAUDE.md")).read() == "# mine\n- keep this\n"
# LLM audit must quote an exact existing line
b2 = curate.new_bullet("always", "decide routine choices yourself", "global", "preference", "user-stated", 2, [{"sid": "x"}], "active")
curate.save([b2])
fake({"items": [{"file": "claude", "action": "remove", "line": "- keep this", "replacement": "", "reason": "dup", "bullet_ids": [b2["id"]]},
                {"file": "claude", "action": "remove", "line": "- not in file", "replacement": "", "reason": "x", "bullet_ids": []}]})
items = instructions.llm_audit()
assert len(items) == 1 and items[0]["edit"]["line"] == "- keep this"

# --- Tier 3: violated rule → verified guard → enforced in PreToolUse, unless the user asked
b3 = curate.new_bullet("about to commit", "commit only when asked", "global", "preference", "observed", 3, [{"sid": "claude-g1"}], "active")
b3.update(violations=2, sessions=["claude-g1"])
curate.save([b3])
for sid, cmd in (("g1", "git commit -m wip"), ("g2", "ls -la"), ("g2", "npm test")):
    hook("claude", {"session_id": sid, "cwd": "/tmp", "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}})
fake({"enforceable": True, "regex": r"^\s*git\s+commit\b", "unless_prompt": r"\bcommit\b", "message": "commit only when the user asked", "action": "block"})
gp = guards.propose_all()
assert gp and gp[0]["type"] == "guard", gp
assert guards.load()[0]["verify"]["replay_hits"] == 1 and guards.load()[0]["verify"]["fp"] == 0
pre = {"session_id": "g3", "cwd": "/tmp", "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "permission_mode": "default"}
assert hook("claude", pre) == {}    # proposed, not active yet
guards.set_status(guards.load()[0]["id"], "active")
hook("claude", {"session_id": "g3", "cwd": "/tmp", "hook_event_name": "UserPromptSubmit", "prompt": "fix the typo"})
g = hook("claude", pre)["hookSpecificOutput"]
assert "permissionDecision" not in g and "commit only when" in g["additionalContext"], g
hook("claude", {"session_id": "g3", "cwd": "/tmp", "hook_event_name": "UserPromptSubmit", "prompt": "ok commit it"})
assert hook("claude", pre) == {}

# --- Tier 2 hunk rules
src = "a\nb\nc\n"
assert patch.apply_hunks(src, [{"old": "b\n", "new": "B\n"}]) == "a\nB\nc\n"
for bad in ([{"old": "zz", "new": "q"}], [{"old": "\n", "new": "x"}]):
    try:
        patch.apply_hunks(src, bad)
        raise AssertionError("should reject")
    except ValueError:
        pass
d = tempfile.mkdtemp()
case = patch.write_case(d, "debug", {"prompt": "tests flaky", "should_fire": True, "reply_regex": "reproduce"})
assert os.path.exists(os.path.join(d, "evals", case, "graders", "trigger.md"))

# --- radar: baseline, then an upstream change to a hot skill is proposed; unrelated change ignored
up = tempfile.mkdtemp()
g = lambda *a: subprocess.run(["git", "-C", up, *a], check=True, capture_output=True)
g("init", "-q"), g("config", "user.email", "t@t"), g("config", "user.name", "t")
os.makedirs(os.path.join(up, "skills", "ship")), os.makedirs(os.path.join(up, "skills", "other"))
open(os.path.join(up, "skills", "ship", "SKILL.md"), "w").write("v1\n")
open(os.path.join(up, "skills", "other", "SKILL.md"), "w").write("v1\n")
g("add", "-A"), g("commit", "-qm", "v1")
srcs = os.path.join(tempfile.mkdtemp(), "sources.json")
json.dump({"sources": [{"name": "t", "repo": up, "paths": ["skills"], "maps": {"ship": "ship", "other": "plan"}}]}, open(srcs, "w"))
os.environ["HONEY_RADAR_SOURCES"] = srcs
assert radar.run() == []
open(os.path.join(up, "skills", "ship", "SKILL.md"), "w").write("v2: verify the remote SHA after push\n")
open(os.path.join(up, "skills", "other", "SKILL.md"), "w").write("v2\n")
g("commit", "-qam", "v2")
fake({"adopt": True, "rationale": "fixes PR confusion", "suggestion": "After pushing, verify the remote SHA."})
props = radar.run()
assert len(props) == 1 and props[0]["skill"] == "ship", props
assert json.load(open(os.path.join(H, "state", "radar-last.json")))["ignored"] == 1
print("tiers ok")
