"""Cross-platform pieces Windows depends on: locks, atomic replace, launcher, guard drive paths."""
import os
import subprocess
import sys
import tempfile
import time

from helpers import REPO
from honey import filelock, install
from honey.guard import check
from honey.rsi import store

tmp = tempfile.mkdtemp()

# A held lock is visible to another process, and released after the block.
lock_path = os.path.join(tmp, "x.lock")
probe = ("import os,sys; sys.path.insert(0, sys.argv[2]); from honey import filelock\n"
         "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR); print(filelock.try_lock(fd))")
with filelock.exclusive(lock_path):
    held = subprocess.run([sys.executable, "-c", probe, lock_path, REPO], capture_output=True, text=True, encoding="utf-8").stdout.strip()
free = subprocess.run([sys.executable, "-c", probe, lock_path, REPO], capture_output=True, text=True, encoding="utf-8").stdout.strip()
assert (held, free) == ("False", "True"), (held, free)

# store.lock non-blocking with a timeout yields False while held elsewhere.
with store.lock() as got:
    assert got is True
    r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from honey.rsi import store\n"
                        "with store.lock(timeout=0.2) as g: print(g)", REPO], capture_output=True, text=True, encoding="utf-8",
                       env=dict(os.environ))
    assert r.stdout.strip() == "False", r.stdout + r.stderr

# Atomic JSON keeps LF bytes on every platform.
fp = os.path.join(tmp, "a.json")
store.write_json(fp, {"k": "é"})
with open(fp, "rb") as f:
    assert b"\r\n" not in f.read()
assert store.read_json(fp) == {"k": "é"}

# The Windows launcher runs bin/honey in place and resolves the repo from the target path.
launcher = os.path.join(tmp, "honey")
with open(launcher, "w", encoding="utf-8") as f:
    f.write(install.launcher(os.path.join(REPO, "bin", "honey")))
r = subprocess.run([sys.executable, launcher, "--help"], capture_output=True, text=True, encoding="utf-8", env=dict(os.environ))
assert r.returncode == 0 and "honey" in (r.stdout + r.stderr).lower(), r.stdout + r.stderr

# Guard treats Windows drive and profile paths as absolute.
assert check("rm -rf C:/")[1] == "deny"
assert check("rm -rf C:/Users/me")[1] == "deny"
assert check("rm -rf $USERPROFILE")[1] == "deny"
assert check("rm -rf build") == (None, None)

print("windows compat ok")
