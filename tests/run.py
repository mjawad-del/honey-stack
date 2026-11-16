#!/usr/bin/env python3
"""Run tests/test_*.py in parallel, each in its own interpreter with a throwaway HONEY_HOME.

Usage: python3 tests/run.py [-j N] [--changed [BASE]] [name ...]
  name        run only these tests (`guard` matches test_guard*.py)
  --changed   run only tests affected by changes vs BASE (default origin/main) plus the working tree;
              anything under tests/helpers.py, tests/run.py or an unmapped honey/ module runs everything
"""
import argparse
import concurrent.futures as cf
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
NONE = ('{"session_summary":"","candidates":[],"bullet_labels":[],"skill_feedback":[],"toolkit_misfires":[],'
        '"progress":[],"project_facts_dropped":0,"none_reason":"test"}')


def run_one(t):
    h = tempfile.mkdtemp(prefix="honey-test-")
    start = time.monotonic()
    try:
        none = os.path.join(h, "none-reflection.json")  # tests never make a paid reflector call
        with open(none, "w", encoding="utf-8") as f:
            f.write(NONE)
        env = dict(os.environ, HONEY_HOME=h, PYTHONDONTWRITEBYTECODE="1", HONEY_FAKE_LLM=none, HONEY_NO_AUTOUPDATE="1")
        env.pop("RSI_CHILD", None), env.pop("HONEY_DISABLE", None)
        r = subprocess.run([sys.executable, t], env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=here)
    finally:
        shutil.rmtree(h, ignore_errors=True)  # a detached RSI job may still be writing; never fail the run on cleanup
    return t, r, time.monotonic() - start


def changed_files(base):
    out = set()
    for args in (["diff", "--name-only", f"{base}...HEAD"], ["diff", "--name-only", "HEAD"],
                 ["ls-files", "--others", "--exclude-standard"]):
        r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 and args[1] == "--name-only" and "..." in args[-1]:
            return None  # unknown base: caller runs everything
        out.update(line for line in r.stdout.splitlines() if line)
    return out


def affected(tests, files):
    """Tests touched by `files`; None means run everything."""
    texts = {}
    for t in tests:
        with open(t, encoding="utf-8") as f:
            texts[t] = f.read()
    picked = set()
    for f in files:
        name = os.path.basename(f)
        if f in ("tests/helpers.py", "tests/run.py"):
            return None
        if f.startswith("tests/") and name.startswith("test_") and f.endswith(".py"):
            picked.update(t for t in tests if os.path.basename(t) == name)
            continue
        stem = os.path.splitext(name)[0]
        if f.startswith("honey/") and f.endswith(".py"):
            mod = f[:-3].replace("/", ".")
            # imported as a module, or driven as a CLI subcommand ("bin/honey", "ship", ...)
            pat = re.compile(rf"\b{re.escape(mod)}\b|from honey(?:\.\w+)* import [^\n]*\b{re.escape(stem)}\b"
                             rf"|[\"']{re.escape(stem)}[\"']")
            hits = {t for t, s in texts.items() if pat.search(s)}
            if not hits or stem in ("__init__", "cli", "dispatch", "paths", "hookio"):
                return None  # shared plumbing or unmapped module: be safe
            picked |= hits
        elif f.startswith(("hooks/", "bin/")):
            return None
        else:  # skills, docs, evals: tests that reference the path or glob its top-level directory
            top = f.split("/", 1)[0] + "/" if "/" in f else None
            picked.update(t for t, s in texts.items() if f in s or (top and f'"{top}' in s or top and f"'{top}" in s
                                                                     or top and f"{top.rstrip('/')}\"" in s))
    return picked


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--changed", nargs="?", const="origin/main", metavar="BASE")
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()

    tests = sorted(glob.glob(os.path.join(here, "test_*.py")))
    if a.names:
        tests = [t for t in tests if any(os.path.basename(t).startswith(f"test_{n.removeprefix('test_').removesuffix('.py')}")
                                         for n in a.names)]
    if a.changed:
        files = changed_files(a.changed)
        picked = affected(tests, files) if files is not None else None
        if picked is not None:
            tests = sorted(picked)
            print(f"--changed {a.changed}: {len(files)} files -> {len(tests)} tests")
            if not tests:
                print("no affected tests")
                return 0
    # Biggest files first so the slowest tests don't start last and stretch the wall time.
    tests.sort(key=os.path.getsize, reverse=True)

    start, failed = time.monotonic(), []
    with cf.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        for t, r, secs in pool.map(run_one, tests):
            ok = r.returncode == 0
            print(("ok   " if ok else "FAIL ") + f"{os.path.basename(t)}  {secs:.1f}s")
            if not ok:
                failed.append((t, r))
    for t, r in failed:
        print(f"\n--- {os.path.basename(t)} ---\n" + (r.stdout + r.stderr)[-3000:])
    total = f"{len(tests)} tests in {time.monotonic() - start:.1f}s"
    print(f"python {sys.version.split()[0]}: {'all passed' if not failed else f'{len(failed)} failed'} ({total})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
