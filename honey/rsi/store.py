"""Tiny persistence layer: append-only JSONL, atomic JSON, a lock, and git commits of ~/.honey."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time

from honey import paths


def ensure_dir(p: str) -> str:
    os.makedirs(p, exist_ok=True)
    return p


def append_jsonl(file: str, obj: dict) -> None:
    ensure_dir(os.path.dirname(file))
    line = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)  # single write with O_APPEND: safe across concurrent hooks
    finally:
        os.close(fd)


def read_jsonl(file: str) -> list:
    out = []
    try:
        with open(file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # tolerate a torn line rather than lose the file
    except FileNotFoundError:
        pass
    return out


def write_json(file: str, obj, sort=False) -> None:
    ensure_dir(os.path.dirname(file))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(file), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=sort)
        f.write("\n")
    os.replace(tmp, file)


def write_jsonl(file: str, rows: list) -> None:
    ensure_dir(os.path.dirname(file))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(file), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, file)


def read_json(file: str, default=None):
    try:
        with open(file, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def write_text(file: str, text: str) -> None:
    ensure_dir(os.path.dirname(file))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(file), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, file)


@contextlib.contextmanager
def session_lock(file):
    """Serialize each session's read/modify/write without blocking other sessions."""
    import fcntl
    ensure_dir(os.path.dirname(file))
    fd = os.open(file + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def lock(blocking=True, timeout=None):
    """Exclusive lock on ~/.honey/.lock. Yields False if non-blocking (or `timeout` seconds pass) while held."""
    import fcntl
    ensure_dir(paths.home())
    fd = os.open(paths.path(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking and deadline is None else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if deadline is None or time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        yield True
    finally:
        os.close(fd)


GITIGNORE = "events/\nstate/\ndigests/\ncache/\nlogs/\nbackups/\n.lock\n.tmp-*\n"


def git_commit(message: str) -> bool:
    """Commit ~/.honey (RSI's own data repo; never a user repo). Returns True if a commit was made."""
    h = paths.home()
    ensure_dir(h)

    def git(*args):
        return subprocess.run(["git", "-C", h, *args], capture_output=True, text=True, timeout=20)

    if not os.path.isdir(os.path.join(h, ".git")):
        git("init", "-q")
        git("config", "user.name", "honey-rsi")
        git("config", "user.email", "honey-rsi@localhost")
        git("config", "commit.gpgsign", "false")
    gi = os.path.join(h, ".gitignore")
    if not os.path.exists(gi):
        write_text(gi, GITIGNORE)
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        return False
    return git("commit", "-q", "--no-verify", "-m", message).returncode == 0
