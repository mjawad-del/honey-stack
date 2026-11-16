"""Sandbox install → uninstall restores everything; later user edits survive uninstall."""
import json
import os
import subprocess
import sys
import tempfile

from helpers import REPO

HONEY = [sys.executable, os.path.join(REPO, "bin", "honey")]
home = tempfile.mkdtemp()
c, x, a = (os.path.join(home, d) for d in (".claude", ".codex", ".agents"))
os.makedirs(os.path.join(c, "skills")), os.makedirs(x), os.makedirs(os.path.join(a, "skills"))
settings = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
    {"type": "command", "command": "$HOME/.claude/hooks/scripts/session-context.py"},
    {"type": "command", "command": "keep-me.sh"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "$HOME/.claude/hooks/scripts/mct-prompt-router.py"}]}]},
    "enabledPlugins": {"superpowers@claude-plugins-official": True, "ponytail@ponytail": True, "other@x": True},
    "statusLine": {"type": "command", "command": "bash /p/plugins/cache/ponytail/ponytail/4.7.0/hooks/s.sh"}}
orig = json.dumps(settings, indent=4) + "\n"
open(os.path.join(c, "settings.json"), "w", encoding="utf-8").write(orig)
open(os.path.join(c, "CLAUDE.md"), "w", encoding="utf-8").write("# old\nMCT loop\n")
open(os.path.join(c, "MCT.md"), "w", encoding="utf-8").write("mct")
toml = '[plugins."ponytail@ponytail"]\nenabled = true\n\n[plugins."keep@x"]\nenabled = true\n'
open(os.path.join(x, "config.toml"), "w", encoding="utf-8").write(toml)
os.symlink("../../gone/tdd", os.path.join(c, "skills", "tdd"))
os.makedirs(os.path.join(c, "skills", "opensrc")); open(os.path.join(c, "skills", "opensrc", "SKILL.md"), "w", encoding="utf-8").write("mct")
os.makedirs(os.path.join(c, "commands")); open(os.path.join(c, "commands", "mct-start.md"), "w", encoding="utf-8").write("x")
os.makedirs(os.path.join(x, "skills", "handoff")); open(os.path.join(x, "skills", "handoff", "SKILL.md"), "w", encoding="utf-8").write("matt")
os.makedirs(os.path.join(a, "skills", "grilling")); open(os.path.join(a, "skills", "grilling", "SKILL.md"), "w", encoding="utf-8").write("g")
os.symlink(os.path.join(a, "skills", "grilling"), os.path.join(c, "skills", "grilling"))


def run(*args):
    extra = ["--slim-instructions", "--retire-legacy"] if args[0] == "install" else []
    r = subprocess.run(HONEY + list(args) + extra + ["--home", home, "--yes"], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


run("install")
s = json.load(open(os.path.join(c, "settings.json"), encoding="utf-8"))
assert s["enabledPlugins"]["superpowers@claude-plugins-official"] is False and s["enabledPlugins"]["other@x"] is True
assert "UserPromptSubmit" not in s["hooks"]
assert [h["command"] for h in s["hooks"]["SessionStart"][0]["hooks"]] == ["keep-me.sh"]
assert "statusline" in s["statusLine"]["command"]
assert 'ponytail@ponytail"]\nenabled = false' in open(os.path.join(x, "config.toml"), encoding="utf-8").read()
assert 'keep@x"]\nenabled = true' in open(os.path.join(x, "config.toml"), encoding="utf-8").read()
assert not os.path.lexists(os.path.join(c, "skills", "tdd")) and not os.path.exists(os.path.join(c, "MCT.md"))
assert not os.path.exists(os.path.join(c, "skills", "opensrc")) and not os.path.exists(os.path.join(x, "skills", "handoff"))
assert not os.path.lexists(os.path.join(c, "skills", "grilling")) and not os.path.exists(os.path.join(c, "commands", "mct-start.md"))
assert "Nothing to do" in run("install")

run("uninstall")
assert open(os.path.join(c, "settings.json"), encoding="utf-8").read() == orig
assert open(os.path.join(x, "config.toml"), encoding="utf-8").read() == toml
assert open(os.path.join(c, "MCT.md"), encoding="utf-8").read() == "mct" and os.readlink(os.path.join(c, "skills", "tdd")) == "../../gone/tdd"
assert open(os.path.join(c, "skills", "opensrc", "SKILL.md"), encoding="utf-8").read() == "mct" and os.path.exists(os.path.join(x, "skills", "handoff"))
assert open(os.path.join(c, "skills", "grilling", "SKILL.md"), encoding="utf-8").read() == "g"  # link recreated, target moved back

# user edits settings after install → uninstall reverts only honey's keys and keeps the edit
run("install")
s = json.load(open(os.path.join(c, "settings.json"), encoding="utf-8"))
s["model"] = "opus"
open(os.path.join(c, "settings.json"), "w", encoding="utf-8").write(json.dumps(s))
out = run("uninstall")
s = json.load(open(os.path.join(c, "settings.json"), encoding="utf-8"))
assert s["model"] == "opus" and s["enabledPlugins"]["ponytail@ponytail"] is True, out
assert any("mct-prompt-router" in h["command"] for g in s["hooks"]["UserPromptSubmit"] for h in g["hooks"])
assert s["statusLine"]["command"].endswith("s.sh")
print("install ok")
