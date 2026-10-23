#!/usr/bin/env python3
"""Linux integration acceptance: existing MySQL/Redis required; isolated TCP instances."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time

from protocol import recv_pkg, require, send_pkg
from test_shutdown import children, configure, connect
from test_worker_snapshot import wait_for


def run_case(binary, source, root, name):
    directory = root / name
    directory.mkdir()
    metrics = directory / "metrics"
    if name == "invalid-directory":
        metrics.write_text("keep", encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = "\n".join(line for line in configure(source, port).splitlines()
                     if not line.strip().startswith(("WorkerSnapshotEnable", "WorkerSnapshotDirectory")))
    config = directory / "nginx.conf"
    config.write_text(base + f"\nWorkerSnapshotEnable = {int(name != 'disabled')}\n"
                      f"WorkerSnapshotDirectory = {metrics}\n", encoding="utf-8")
    proc = None
    worker_pids = []
    log = directory / "server.log"
    def snapshots():
        return [json.loads(file.read_text()) for file in metrics.glob("worker-*.json")]
    try:
        with (directory / "console.log").open("wb") as output:
            proc = subprocess.Popen([str(binary)], cwd=directory, stdout=output,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            def ready():
                require(proc.poll() is None, f"{name}: server exited during startup; see {directory}")
                return log.exists() and log.read_text(errors="replace").count("【worker进程】启动") == 2
            wait_for(ready, timeout=30)
            worker_pids = children(proc.pid)
            if name == "enabled":
                initial = wait_for(lambda: snapshots() if len(snapshots()) == 2 else None)
                require({s["pid"] for s in initial} == set(worker_pids), "snapshot PIDs do not match Workers")
                require({s["worker_index"] for s in initial} == {0, 1}, "incorrect Worker indices")
                require({s["master_pid"] for s in initial} == {proc.pid}, "incorrect Master identity")
                require(len({s["master_start_ticks"] for s in initial}) == 1, "inconsistent Master start identity")
            with connect(port, "127.0.9.1") as client:
                send_pkg(client, 0, b"")
                require(recv_pkg(client) == (0, b""), "TCP Ping failed")
                if name == "enabled":
                    wait_for(lambda: sum(s["connections_current"] for s in snapshots()) == 1)
            if name == "enabled":
                wait_for(lambda: sum(s["connections_current"] for s in snapshots()) == 0)
                files = list(metrics.glob("*.json"))
                for file in files:
                    file.unlink()
                    file.mkdir()  # Force rename failure without changing process privileges.
                wait_for(lambda: log.read_text(errors="replace").count("publication failed;") >= 2)
                with connect(port, "127.0.9.2") as client:
                    send_pkg(client, 0, b"")
                    require(recv_pkg(client) == (0, b""), "snapshot write failure interrupted TCP")
                for file in files:
                    file.rmdir()
                def recovered():
                    values = snapshots()
                    return len(values) == 2 and all(s["write_failures_total"] >= 1 for s in values)
                wait_for(recovered)
            elif name == "invalid-directory":
                require(metrics.read_text() == "keep", "invalid directory target was modified")
                require(log.read_text(errors="replace").count("publisher initialization failed;") == 2,
                        "missing initialization failure warnings")
            else:
                require(not metrics.exists(), "disabled publisher created metrics directory")
            proc.send_signal(signal.SIGTERM)
            require(proc.wait(timeout=30) == 0, f"{name}: Master did not stop cleanly")
            require(all(not Path(f"/proc/{pid}").exists() for pid in worker_pids), "Worker remains after stop")
            if name == "enabled":
                require(not list(metrics.iterdir()), "snapshot files remain after clean stop")
            print(f"PASS: {name}", flush=True)
    finally:
        if proc is not None:
            if proc.poll() is None or any(Path(f"/proc/{pid}").exists() for pid in worker_pids):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait(timeout=5)
        config.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    require(Path("/proc/self/stat").exists(), "this test requires Linux")
    binary = args.binary.resolve(strict=True)
    source = binary.with_name("nginx.conf").read_text(encoding="utf-8")
    root = Path(tempfile.mkdtemp(prefix="epoll-snapshot-live-"))
    print(f"Test logs: {root}", flush=True)
    for name in ("enabled", "invalid-directory", "disabled"):
        run_case(binary, source, root, name)


if __name__ == "__main__":
    main()
