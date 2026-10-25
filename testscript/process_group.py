"""Lifecycle of test processes started in a private Linux session."""
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time


def _stat(pid):
    path = Path(f"/proc/{pid}/stat")
    try:
        return path.read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None


def start_process_group(command, **kwargs):
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    fields = _stat(process.pid)
    process.group_start_ticks = int(fields[19]) if fields else None
    process.group_stopped = False
    return process


def _members(process):
    leader = _stat(process.pid)
    if leader and int(leader[19]) != process.group_start_ticks:
        raise RuntimeError(f"process group identity changed: {process.pid}")
    members = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.geteuid():
                continue
            fields = _stat(path.name)
        except (FileNotFoundError, ProcessLookupError):
            continue
        if fields and int(fields[2]) == process.pid:
            if int(fields[3]) != process.pid:
                raise RuntimeError(f"process group is not a private session: {process.pid}")
            # Orphan zombies hold no files or sockets and can only be reaped by their parent.
            if fields[0] not in ("Z", "X", "x"):
                members.append(int(path.name))
    return members


def stop_process_group(process, timeout=30):
    """Confirm the whole owned group stopped, including children of an exited leader."""
    if process.group_stopped:
        return
    for signo, seconds in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5)):
        if _members(process):
            try:
                os.killpg(process.pid, signo)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + seconds
        while True:
            process.poll()
            if not _members(process):
                process.wait(timeout=5)
                process.group_stopped = True
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    raise RuntimeError(f"test process group still alive: {process.pid}; preserve its directory")


def run_process_group(command, *, timeout, **kwargs):
    process = start_process_group(command, **kwargs)
    try:
        code = process.wait(timeout=timeout)
        if code:
            raise subprocess.CalledProcessError(code, command)
        return subprocess.CompletedProcess(command, code)
    finally:
        stop_process_group(process)


def remove_process_directory(directory, processes):
    """Retain test files if any owned process group has not been confirmed stopped."""
    if any(not process.group_stopped for process in processes):
        raise RuntimeError(f"test processes not confirmed stopped; directory preserved: {directory}")
    shutil.rmtree(directory)
