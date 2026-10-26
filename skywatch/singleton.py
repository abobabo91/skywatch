"""One daemon per machine, enforced by the daemon itself.

The supervisor's "is it already running?" check is a heuristic and heuristics
fail under load: on 2026-10-26 a 30 s `tasklist` timeout on a swapping laptop
was read as "not running" every 5 minutes, and 63 daemons piled up, each
recomputing the whole catalog and fighting over the Telegram long-poll (HTTP
409). This lock is the hard guarantee: a second daemon exits before it loads
anything, whatever the supervisor believed.

Windows: a named kernel mutex, released automatically when the process dies.
Elsewhere: an flock on a file in the data directory, likewise released on exit.
"""
from __future__ import annotations

import os
import sys

from . import config

MUTEX_NAME = r"Local\skywatch-daemon"
LOCK_FILE = config.DATA_DIR / "skywatch.lock"

_handle = None   # keep the handle/file alive for the life of the process


def held_by_another() -> bool:
    """True if a live process already holds the daemon lock."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wintypes.HANDLE
        h = k32.CreateMutexW(None, False, MUTEX_NAME)
        if not h:
            return False
        exists = ctypes.get_last_error() == 183 or k32.GetLastError() == 183   # ERROR_ALREADY_EXISTS
        k32.CloseHandle(h)
        return exists
    import fcntl
    try:
        with open(LOCK_FILE, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def acquire() -> bool:
    """Take the lock for this process. False if another daemon holds it."""
    global _handle
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.SetLastError(0)
        h = k32.CreateMutexW(None, True, MUTEX_NAME)
        if not h:
            return True            # cannot create a mutex at all; do not block the daemon
        if k32.GetLastError() == 183:
            k32.CloseHandle(h)
            return False
        _handle = h
        return True
    import fcntl
    f = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    f.seek(0); f.truncate(); f.write(str(os.getpid())); f.flush()
    _handle = f
    return True
