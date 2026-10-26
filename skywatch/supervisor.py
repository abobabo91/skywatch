"""Keep exactly one daemon alive.

Task Scheduler runs this every few minutes. If the daemon is already up it exits
immediately; if it is not, it starts one. That covers reboots, crashes and the
machine waking from sleep without needing the task itself to be long-running.

Two rules, both learned on 2026-10-26 when 63 daemons accumulated on a swapping
laptop:

* No subprocess in the liveness check. The old `tasklist` call had a 30 s
  timeout, the timeout fired under load, and "unknown" was treated as "not
  running". The check is now an in-process Win32 call that takes microseconds,
  and anything that cannot be decided counts as RUNNING -- a missed restart costs
  five minutes, a false start costs a duplicate forever.
* The daemon enforces single-instance itself (`skywatch.singleton`), so even a
  wrong answer here cannot produce two of them.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from . import config, singleton

PID_FILE = config.DATA_DIR / "skywatch.pid"
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)

STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _pid_running(pid: int) -> bool:
    """True if a process with this pid is alive and is a python interpreter.

    Windows: OpenProcess + GetExitCodeProcess + QueryFullProcessImageNameW, no
    shell-out. Any failure to decide returns True (see module docstring).
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        # ERROR_INVALID_PARAMETER (87) = no such process. Anything else (access
        # denied etc.) means a process exists that we may not inspect: assume ours.
        return k32.GetLastError() != 87
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return False
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return True
        return "python" in buf.value.lower()
    finally:
        k32.CloseHandle(h)


def read_pid() -> int:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def is_running() -> bool:
    return singleton.held_by_another() or _pid_running(read_pid())


def start_daemon() -> int:
    """Launch the daemon detached, so it outlives this supervisor process."""
    cmd = [sys.executable, "-m", "skywatch.daemon"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        cmd, cwd=str(config.PROJECT_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        # DETACHED_PROCESS and CREATE_NO_WINDOW are mutually exclusive; detaching
        # is what keeps the daemon alive after Task Scheduler reaps this process.
        creationflags=DETACHED | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    PID_FILE.write_text(str(proc.pid))
    return proc.pid


def main() -> int:
    if is_running():
        print(f"skywatch daemon already running (pid {read_pid()})")
        return 0
    pid = start_daemon()
    # Give it a moment and confirm it did not die on startup.
    time.sleep(5)
    if _pid_running(pid):
        print(f"started skywatch daemon (pid {pid})")
        return 0
    print("daemon failed to stay up; see data/skywatch.log", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
