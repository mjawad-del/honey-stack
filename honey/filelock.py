"""Advisory exclusive file locks on POSIX (flock) and Windows (msvcrt byte-range lock on byte 0)."""
from __future__ import annotations

import contextlib
import os
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


def try_lock(fd: int) -> bool:
    """Take the lock without waiting. False when another process holds it."""
    if fcntl:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def lock(fd: int) -> None:
    """Wait until the lock is free, then take it."""
    if fcntl:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    while not try_lock(fd):
        time.sleep(0.05)


def unlock(fd: int) -> None:
    """Release explicitly; Windows does not promise release on close."""
    if fcntl:
        return  # flock is released when the descriptor closes
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextlib.contextmanager
def exclusive(path: str):
    """Hold a blocking exclusive lock on `path` (created if missing) for the block."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        lock(fd)
        try:
            yield
        finally:
            unlock(fd)
    finally:
        os.close(fd)
