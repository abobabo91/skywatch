"""Keep exactly one daemon alive.

Task Scheduler runs this every few minutes. If the daemon is already up it exits
immediately; if it is not, it starts one. That covers reboots, crashes and the
machine waking from sleep without needing the task itself to be long-running.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import config

PID_FILE = config.DATA_DIR / "skywatch.pid"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)


def _pid_running(pid: int) -> bool:
    """True if a process with this pid exists and looks like our daemon."""
    if pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30,
            creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return str(pid) in out and "python" in out.lower()


def read_pid() -> int:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def is_running() -> bool:
    return _pid_running(read_pid())


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
