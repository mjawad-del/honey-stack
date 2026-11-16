from helpers import REPO  # noqa: F401
from honey.guard import check
from honey import modes

assert check("git --no-pager push -f origin main")[1] == "ask"
assert check("rm -rf ~")[1] == "deny"
assert check('bash -lc "rm -rf /"')[1] == "deny"
assert check('echo "rm -rf /"') == (None, None)
assert check("git checkout -- src/app.py") == (None, None)
assert check("git checkout -- .")[1] == "ask"
assert check("ls -la") == (None, None)
assert check(None) == (None, None)
assert check("echo a > /tmp/x > /tmp/y > /tmp/z") == (None, None)   # redirect scan terminates
assert check("cat x > /dev/disk0")[1] == "deny"
assert check("git push origin +main")[1] == "ask"
assert check("git checkout .")[1] == "ask"
assert check("git branch --delete --force feat")[1] == "ask" and check("git branch -d -f feat")[1] == "ask"
assert check("git branch -d feat") == (None, None)
assert check("kubectl -n prod delete pod x")[1] == "ask"
assert check("terraform -chdir=infra destroy")[1] == "ask"
assert check("kubectl get pods") == (None, None)
assert modes.parse_command("/honey explain") == ["explain"]
assert modes.parse_command("honey off") == ["off"]
assert modes.parse_command("stop honey.") == ["off"]
assert modes.parse_command("add a normal mode toggle") is None
assert modes.parse_command("/honey how do I debug this") is None
assert modes.parse_command("honey default direct") == ["default", "direct"]
# absolute rm -rf: cleanup inside the workspace or a temp dir passes; roots, home and escapes still warn (#55, #58)
import os, tempfile
ws = "/Users/dev/code/app"
assert check("rm -rf /Users/dev/code/app/node_modules /Users/dev/code/app/dist", cwd=ws) == (None, None)
assert check("rm -rf /tmp/build-cache", cwd=ws) == (None, None)
assert check("rm -rf /private/tmp/x/y") == (None, None)
assert check(f"rm -rf {os.path.join(tempfile.gettempdir(), 'scratch')}") == (None, None)
assert check(f"rm -rf {ws}", cwd=ws)[1] == "deny"                      # the workspace itself
assert check(f"rm -rf {ws}/*", cwd=ws)[1] == "deny"                    # everything in it
assert check(f"rm -rf {ws}/../other", cwd=ws)[1] == "deny"             # escape via ..
assert check("rm -rf /Users/dev/code/app2", cwd=ws)[1] == "deny"       # sibling prefix
assert check("rm -rf /tmp", cwd=ws)[1] == "deny" and check("rm -rf /tmp/*")[1] == "deny"
assert check("rm -rf /tmp/../etc")[1] == "deny"
assert check("rm -rf /Users/dev/Documents", cwd="/Users/dev")[1] == "deny"  # cwd = home is no exemption
assert check("rm -rf /etc/x", cwd="/")[1] == "deny"
assert check("rm -rf /Users/dev/code/app/build")[1] == "deny"          # no cwd known
assert check("rm -rf /tmp/x | xargs rm -rf", cwd=ws)[1] == "deny"
# find -delete: same scoped-cleanup rule as rm; only the start paths count
assert check(f"find {ws}/dist -name '*.map' -delete", cwd=ws) == (None, None)
assert check("find /tmp/cache -type f -delete") == (None, None)
assert check("find . -name '*.pyc' -delete") == (None, None)
assert check(f"find {ws} -delete", cwd=ws)[1] == "deny"
assert check("find / -name core -delete", cwd=ws)[1] == "deny"
assert check("find ~ -name x -delete")[1] == "deny"
# heredoc bodies are data unless fed to a shell
assert check("python3 - <<'EOF'\nprint('rm -rf /')\nx = \"unbalanced\nEOF") == (None, None)
assert check("cat > notes.md <<EOF\nrun rm -rf / to wipe it\nEOF") == (None, None)
assert check("bash <<'EOF'\nrm -rf /\nEOF")[1] == "deny"
assert check("cat <<'EOF' | sh\nrm -rf ~\nEOF")[1] == "deny"
assert check("python3 - <<'EOF'\nprint(1)\nEOF\nrm -rf ~")[1] == "deny"     # command after the heredoc
# unparseable command (shlex fails): fallback applies the same scoped rule
assert check('rm -rf /tmp/x; echo "unterminated', cwd=ws) == (None, None)
assert check(f'rm -rf {ws}/build && echo "oops', cwd=ws) == (None, None)
assert check('rm -rf /etc/x; echo "unterminated', cwd=ws)[1] == "deny"
assert check('rm -rf ~; echo "x')[1] == "deny"
assert check('rm -rf /; echo "x')[1] == "deny"
# fail closed: quoted <<, missing terminator, eval/$( ) consumers, find global options
assert check("echo '<<EOF'\nrm -rf /\nEOF")[1] == "deny"
assert check("echo \"x <<EOF\"\nrm -rf ~")[1] == "deny"
assert check("cat <<EOF\nrm -rf /")[1] == "deny"                         # no terminator
assert check('eval "$(cat <<EOF\nrm -rf /\nEOF\n)"')[1] == "deny"
assert check("source /dev/stdin <<EOF\nrm -rf ~\nEOF")[1] == "deny"
assert check("find -L / -name x -delete")[1] == "deny"
assert check("find -O3 -D exec ~ -delete")[1] == "deny"
assert check("find -H /tmp/cache -delete") == (None, None)
assert check("echo hi # <<EOF\nrm -rf /\nEOF")[1] == "deny"         # << inside a comment is no heredoc
assert check("cat <<EOF\nhello $(rm -rf /)\nEOF")[1] == "deny"        # unquoted delimiter expands $( )
assert check("cat <<EOF\nhello `rm -rf ~`\nEOF")[1] == "deny"
assert check("cat <<'EOF'\nhello $(rm -rf /)\nEOF") == (None, None)   # quoted delimiter: literal data
assert check("cat > a.txt <<EOF\nbuilt on $(date)\nEOF") == (None, None)
assert check("cat <<'EOF' |bash;\nrm -rf /\nEOF")[1] == "deny"     # consumer followed by a metachar
assert check("cat <<'EOF' | sh)\nrm -rf ~\nEOF")[1] == "deny"
assert check("echo $'a\\' <<EOF'\nrm -rf /\nEOF")[1] == "deny"      # $'...' hides << from bash
import time
nested = "".join(f"bash <<E{i}\n" for i in range(30)) + "echo hi\n" + "".join(f"E{i}\n" for i in reversed(range(30)))
t0 = time.time(); r = check(nested); assert time.time() - t0 < 1 and r[1] == "deny", r   # bounded work, fails closed
# Windows-shaped paths (Git Bash on Windows): drive letters and backslash cwd from the hook payload
assert check("rm -rf C:/src/work/proj/dist", cwd="C:\\src\\work\\proj") == (None, None)
assert check("rm -rf C:/src/work/proj", cwd="C:\\src\\work\\proj")[1] == "deny"
assert check("rm -rf C:/src/work/other", cwd="C:\\src\\work\\proj")[1] == "deny"
assert check("rm -rf C:/work/proj/dist", cwd="C:\\work\\proj")[1] == "deny"   # 2 levels below the drive: too shallow
# a temp variable pointing at a filesystem or drive root must not exempt every path
for var, value in (("TMPDIR", "/"), ("TEMP", "C:\\"), ("TMPDIR", "C:")):
    saved = os.environ.get(var)
    os.environ[var] = value
    try:
        assert check("rm -rf /etc/x")[1] == "deny" and check("rm -rf C:/Windows/x")[1] == "deny", (var, value)
    finally:
        os.environ.pop(var) if saved is None else os.environ.__setitem__(var, saved)
print("guard+modes ok")
