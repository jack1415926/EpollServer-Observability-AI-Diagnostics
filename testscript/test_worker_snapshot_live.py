#!/usr/bin/env python3
"""Linux integration acceptance: existing MySQL/Redis required; isolated TCP instances."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import shutil
import tempfile
import time

from protocol import recv_pkg, require, send_pkg
from process_group import start_process_group, stop_process_group
from test_shutdown import children, configure, connect
from test_worker_snapshot import wait_for


def run_case(binary, source, root, name, collector=None):
    directory = root / name
    directory.mkdir()
    metrics = directory / "metrics"
    if name == "restart-isolation":
        metrics.mkdir(mode=0o700)
        for file in (root / "worker-crash" / "metrics").glob("worker-*.json"):
            shutil.copy2(file, metrics / file.name)
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
    config.chmod(0o600)
    proc = None
    worker_pids = []
    log = directory / "server.log"
    def snapshots():
        return [json.loads(file.read_text()) for file in metrics.glob("worker-*.json")]
    try:
        with (directory / "console.log").open("wb") as output:
            proc = start_process_group([str(binary)], cwd=directory, stdout=output,
                                       stderr=subprocess.STDOUT)
            def ready():
                require(proc.poll() is None, f"{name}: server exited during startup; see {directory}")
                return log.exists() and log.read_text(errors="replace").count("【worker进程】启动") == 2
            wait_for(ready, timeout=30)
            worker_pids = children(proc.pid)
            if name in ("enabled", "worker-crash", "restart-isolation"):
                def current_snapshots():
                    return [s for s in snapshots() if s["pid"] in worker_pids]
                initial = wait_for(lambda: current_snapshots() if len(current_snapshots()) == 2 else None)
                require({s["pid"] for s in initial} == set(worker_pids), "snapshot PIDs do not match Workers")
                require({s["worker_index"] for s in initial} == {0, 1}, "incorrect Worker indices")
                require({s["master_pid"] for s in initial} == {proc.pid}, "incorrect Master identity")
                require(len({s["master_start_ticks"] for s in initial}) == 1, "inconsistent Master start identity")
                def membership():
                    files = list(metrics.glob("master-*.json"))
                    require(len(files) == 1, "expected one Master membership file")
                    return json.loads(files[0].read_text())
                roster = membership()
                require(roster["state"] == "running" and roster["expected_workers"] == 2,
                        "incorrect Master lifecycle state")
                require(roster["master_pid"] == proc.pid and
                        roster["master_start_ticks"] == initial[0]["master_start_ticks"] and
                        roster["boot_id"] == initial[0]["boot_id"], "Master identity mismatch")
                require({(w["worker_index"], w["pid"], w["process_start_ticks"]) for w in roster["workers"]} ==
                        {(w["worker_index"], w["pid"], w["process_start_ticks"]) for w in initial},
                        "Master membership does not match Worker identities")
                require(stat.S_IMODE(next(metrics.glob("master-*.json")).stat().st_mode) == 0o600,
                        "incorrect Master file permissions")
                def collect():
                    run = subprocess.run([str(collector), str(next(metrics.glob("master-*.json")))],
                                         capture_output=True, text=True, timeout=5)
                    require(run.returncode in (0, 2, 3), "collector CLI failed")
                    value = json.loads(run.stdout)
                    require(run.returncode == (0 if value["status"] == "complete" else
                                               2 if value["status"] in ("partial", "stopping") else 3),
                            "collector exit code does not match status")
                    (directory / f"collector-{value['status']}.json").write_text(json.dumps(value, indent=2) + "\n")
                    return value
                if collector:
                    require(collect()["status"] == "complete", "collector cannot read live instance")
            with connect(port, "127.0.9.1") as client:
                send_pkg(client, 0, b"")
                require(recv_pkg(client) == (0, b""), "TCP Ping failed")
                if name == "enabled":
                    wait_for(lambda: sum(s["connections_current"] for s in snapshots()) == 1)
                    if collector:
                        wait_for(lambda: collect()["totals"]["connections_current"] == 1)
            if name == "enabled":
                wait_for(lambda: sum(s["connections_current"] for s in snapshots()) == 0)
                files = list(metrics.glob("worker-*.json"))
                for file in files:
                    file.unlink()
                    file.mkdir()  # Force rename failure without changing process privileges.
                wait_for(lambda: log.read_text(errors="replace").count("publication failed;") >= 2)
                if collector:
                    failed = collect()
                    require(failed["status"] == "unavailable" and failed["valid_workers"] == 0 and
                            failed["totals"]["connections_current"] is None,
                            "unreadable Worker files produced a fabricated total")
                with connect(port, "127.0.9.2") as client:
                    send_pkg(client, 0, b"")
                    require(recv_pkg(client) == (0, b""), "snapshot write failure interrupted TCP")
                for file in files:
                    file.rmdir()
                def recovered():
                    values = snapshots()
                    return len(values) == 2 and all(s["write_failures_total"] >= 1 for s in values)
                wait_for(recovered)
                if collector:
                    wait_for(lambda: collect()["status"] == "complete")
                    os.kill(worker_pids[0], signal.SIGSTOP)
                    try:
                        def expired():
                            value = collect()
                            return (value["status"] == "partial" and value["valid_workers"] == 1 and
                                    any(w["pid"] == worker_pids[0] and w["status"] == "stale"
                                        for w in value["workers"]) and
                                    value["totals"]["connections_current"] is None)
                        wait_for(expired, timeout=8)
                    finally:
                        os.kill(worker_pids[0], signal.SIGCONT)
                    wait_for(lambda: collect()["status"] == "complete")
            elif name == "invalid-directory":
                require(metrics.read_text() == "keep", "invalid directory target was modified")
                require(log.read_text(errors="replace").count("publisher initialization failed;") == 3,
                        "missing initialization failure warnings")
            elif name == "worker-crash":
                # Hold the sibling alive to observe fail-stop membership before cleanup.
                os.kill(worker_pids[1], signal.SIGSTOP)
                os.kill(worker_pids[0], signal.SIGKILL)
                def reaped():
                    roster = membership()
                    return (roster["state"] == "stopping" and
                            [w["pid"] for w in roster["workers"]] == [worker_pids[1]])
                wait_for(reaped)
                require(any(s["pid"] == worker_pids[0] for s in snapshots()),
                        "crashed Worker's snapshot should remain for stale-data testing")
                if collector:
                    require(collect()["status"] == "stopping", "stopping instance reported as complete")
                os.kill(worker_pids[1], signal.SIGCONT)
            elif name == "restart-isolation":
                require(len(snapshots()) == 3, "old Worker residue missing from restart fixture")
                if collector:
                    value = collect()
                    require(value["valid_workers"] == 2 and len(value["workers"]) == 2,
                            "old instance residue included in current totals")
            else:
                require(not metrics.exists(), "disabled publisher created metrics directory")
            if name != "worker-crash":
                if name == "enabled":
                    os.kill(worker_pids[1], signal.SIGSTOP)
                proc.send_signal(signal.SIGTERM)
                if name == "enabled":
                    wait_for(lambda: membership()["state"] == "stopping")
                    os.kill(worker_pids[1], signal.SIGCONT)
            require(proc.wait(timeout=30) == (1 if name == "worker-crash" else 0),
                    f"{name}: unexpected Master exit status")
            require(all(not Path(f"/proc/{pid}").exists() for pid in worker_pids), "Worker remains after stop")
            if collector and name in ("enabled", "worker-crash", "restart-isolation"):
                run = subprocess.run([str(collector), str(metrics / f"master-{roster['boot_id']}-{proc.pid}-{roster['master_start_ticks']}.json")],
                                     capture_output=True, text=True, timeout=5)
                require(run.returncode == 3 and json.loads(run.stdout)["status"] == "unavailable",
                        "exited instance still reported available")
            if name == "enabled":
                require(not list(metrics.iterdir()), "snapshot files remain after clean stop")
            elif name == "worker-crash":
                require(not list(metrics.glob("master-*.json")), "Master file remains after fail-stop")
                require(len(list(metrics.glob("worker-*.json"))) == 1, "expected only crashed Worker residue")
            elif name == "restart-isolation":
                require(len(list(metrics.iterdir())) == 1, "restart cleanup removed old residue or left current files")
            print(f"PASS: {name}", flush=True)
    finally:
        if proc is not None:
            stop_process_group(proc)
        config.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--collector", type=Path, help="optional C++ reader CLI for end-to-end stage 1 acceptance")
    args = parser.parse_args()
    require(Path("/proc/self/stat").exists(), "this test requires Linux")
    binary = args.binary.resolve(strict=True)
    collector = args.collector.resolve(strict=True) if args.collector else None
    source = binary.with_name("nginx.conf").read_text(encoding="utf-8")
    root = Path(tempfile.mkdtemp(prefix="epoll-snapshot-live-"))
    print(f"Test logs: {root}", flush=True)
    for name in ("enabled", "invalid-directory", "disabled", "worker-crash", "restart-isolation"):
        run_case(binary, source, root, name, collector)


if __name__ == "__main__":
    main()
