#!/usr/bin/env python3
"""Linux integration check: starts isolated two-Worker servers using an existing DB/Redis setup."""
import argparse
import concurrent.futures
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

from protocol import recv_pkg, require, send_pkg


def configure(source, port):
    overrides = {
        "Daemon": 0, "WorkerProcesses": 2, "ProcMsgRecvWorkThreadCount": 4,
        "ListenPortCount": 1, "ListenPort0": port, "worker_connections": 256,
        "Sock_RecyConnectionWaitTime": 0, "Sock_WaitTimeEnable": 1,
        "Sock_MaxWaitTime": 5, "Sock_TimeOutKick": 0,
        "Log": "server.log", "LogLevel": 8,
    }
    lines = []
    for line in source.splitlines():
        match = re.match(r"\s*([A-Za-z0-9_]+)\s*=", line)
        if match and match[1] in overrides:
            continue
        lines.append(line)
    lines.extend(f"{key} = {value}" for key, value in overrides.items())
    return "\n".join(lines) + "\n"


def children(pid):
    path = Path(f"/proc/{pid}/task/{pid}/children")
    return [int(value) for value in path.read_text().split()] if path.exists() else []


def connect(port, source_ip):
    sock = socket.socket()
    sock.settimeout(5)
    try:
        sock.bind((source_ip, 0))
        sock.connect(("127.0.0.1", port))
        return sock
    except Exception:
        sock.close()
        raise


def churn(port, index):
    # Distinct loopback IPs avoid the existing 20 requests/IP/minute rate limit.
    with connect(port, f"127.0.1.{index + 1}") as sock:
        send_pkg(sock, 0, b"")
        if index % 2:
            require(recv_pkg(sock) == (0, b""), "unexpected Ping response during churn")
        else:
            # Close with RST while the send/receive/cleanup threads may still be active.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def run_case(binary, source, root, name, signo, crash=False):
    directory = root / name
    directory.mkdir()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    config = directory / "nginx.conf"
    config.write_text(configure(source, port), encoding="utf-8")
    proc = None
    worker_pids = []
    try:
        with (directory / "console.log").open("wb") as output:
            proc = subprocess.Popen([str(binary)], cwd=directory, stdout=output,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                require(proc.poll() is None, f"{name}: server exited during startup")
                worker_pids = children(proc.pid)
                log = directory / "server.log"
                text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
                if len(worker_pids) == 2 and text.count("【worker进程】启动") >= 2:
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError(f"{name}: Workers did not become ready in 30 seconds")

            with connect(port, "127.0.2.1") as idle:
                send_pkg(idle, 0, b"")
                require(recv_pkg(idle) == (0, b""), "startup Ping failed")
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda index: churn(port, index), range(64)))
                require(proc.poll() is None, "server crashed during connection churn")
                # Partial packets must be released during shutdown too.
                idle.sendall(b"\0\x08\0")
                if crash:
                    os.kill(worker_pids[0], signal.SIGKILL)
                else:
                    proc.send_signal(signo)
                require(proc.wait(timeout=30) == (1 if crash else 0),
                        f"{name}: unexpected Master exit code {proc.returncode}")
            require(all(not Path(f"/proc/{pid}").exists() for pid in worker_pids),
                    f"{name}: Worker still exists (running or zombie)")
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
                probe.listen()
            text = (directory / "server.log").read_text(encoding="utf-8", errors="replace")
            require(text.count("shutdown complete") == (1 if crash else 2),
                    f"{name}: missing Worker cleanup completion logs")
            print(f"PASS: {name}, Workers reaped, port released")
    finally:
        if proc is not None:
            # Only the isolated process group created by this test is targeted.
            if proc.poll() is None or any(Path(f"/proc/{pid}").exists() for pid in worker_pids):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait(timeout=5)
        config.unlink(missing_ok=True) # don't retain copied database credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="defaults to nginx.conf next to binary")
    args = parser.parse_args()
    require(sys.platform.startswith("linux"), "this test requires Linux /proc and POSIX signals")
    binary = args.binary.resolve(strict=True)
    require(os.access(binary, os.X_OK), "server binary is not executable")
    source = (args.config or binary.with_name("nginx.conf")).read_text(encoding="utf-8")
    root = Path(tempfile.mkdtemp(prefix="epoll-shutdown-"))
    print(f"Test logs: {root}", flush=True)
    for name, signo, crash in (("term", signal.SIGTERM, False),
                              ("quit", signal.SIGQUIT, False),
                              ("int", signal.SIGINT, False),
                              ("worker-crash", signal.SIGTERM, True)):
        run_case(binary, source, root, name, signo, crash)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
