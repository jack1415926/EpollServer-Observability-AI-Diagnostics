#!/usr/bin/env python3
"""Small, alternating snapshot off/on experiment; not the phase 0 capacity workload."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import statistics
import subprocess
import sys
import tempfile

from protocol import require
from test_shutdown import children, configure
from test_worker_snapshot import wait_for

ROOT = Path(__file__).resolve().parents[1]


def configuration(source, port, enabled):
    overrides = {"WorkerSnapshotEnable": int(enabled), "WorkerSnapshotDirectory": "metrics",
                 "RedisRateLimitEnable": 1, "RedisRateLimitWindowSec": 60,
                 "RedisRateLimitMaxRequests": 100000000}
    lines = []
    for line in configure(source, port).splitlines():
        match = re.match(r"\s*([A-Za-z0-9_]+)\s*=", line)
        if not match or match[1] not in overrides:
            lines.append(line)
    return "\n".join(lines + [f"{key} = {value}" for key, value in overrides.items()]) + "\n"


def run(binary, source, root, index, enabled, server_cpus, client_cpus):
    directory = root / f"{index}-{'on' if enabled else 'off'}"
    directory.mkdir(mode=0o700)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    config = directory / "nginx.conf"
    config.write_text(configuration(source, port, enabled))
    config.chmod(0o600)
    proc = None
    workers = []
    try:
        with (directory / "console.log").open("wb") as output:
            proc = subprocess.Popen(["taskset", "-c", server_cpus, str(binary)], cwd=directory,
                                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            log = directory / "server.log"
            def ready():
                require(proc.poll() is None, f"server exited; see {directory}")
                return log.exists() and log.read_text(errors="replace").count("【worker进程】启动") == 2
            wait_for(ready, timeout=30)
            workers = children(proc.pid)
            require(len(workers) == 2, "expected two isolated Workers")
            for label, requests in (("warmup", 500), ("measured", 3000)):
                load = ["taskset", "-c", client_cpus, sys.executable, str(ROOT / "testscript/tcp_stress_test.py"),
                        "--port", str(port), "--timeout", "5", "--concurrency", "20", "--processes", "2",
                        "--requests-per-client", str(requests), "--source-ip", f"127.0.10.{index}",
                        "--receive-mode", "into", "--validation-mode", "strict", "--socket-mode", "kernel-timeout",
                        "--latency-mode", "sampled", "--latency-sample-every", "100",
                        "--label", directory.name + "-" + label, "--build-mode", "debug",
                        "--server-workers", "2", "--worker-threads", "4", "--rate-limit-mode", "enabled",
                        "--json-out", str(directory / f"{label}.json")]
                command = [sys.executable, str(ROOT / "testscript/performance_sampler.py"),
                           "--master-pid", str(proc.pid), "--label", directory.name + "-" + label,
                           "--output", str(directory / f"resources-{label}.json"), "--log", str(log), "--", *load]
                with (directory / f"{label}.log").open("wb") as output:
                    subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=True, timeout=60)
            result = json.loads((directory / "measured.json").read_text())["result"]
            require(result["successful"] == 60000 and result["failed"] == 0, "small experiment has request failures")
            files = list((directory / "metrics").glob("worker-*.json"))
            if enabled:
                require(len(files) == 2 and all(json.loads(f.read_text())["sequence"] >= 2 for f in files),
                        "sampling did not run during enabled experiment")
            else:
                require(not (directory / "metrics").exists(), "disabled run generated snapshots")
            proc.send_signal(signal.SIGTERM)
            require(proc.wait(timeout=30) == 0, "Master did not exit cleanly")
            require(all(not Path(f"/proc/{pid}").exists() for pid in workers), "Worker remains")
            require(not list((directory / "metrics").glob("*.json")), "clean stop left snapshots")
            print(f"PASS: {directory.name}: {result['successful']} Ping, QPS={result['qps']}, P95={result['latency_ms']['p95']} ms", flush=True)
            return {"enabled": enabled, "run": directory.name, **result}
    finally:
        if proc is not None:
            if proc.poll() is None or any(Path(f"/proc/{pid}").exists() for pid in workers):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait(timeout=5)
        config.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="existing directory for a unique private experiment subdirectory")
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    cpus = sorted(os.sched_getaffinity(0))
    require(len(cpus) >= 4, "small experiment requires four available CPUs")
    server_cpus = ",".join(map(str, cpus[:2]))
    client_cpus = ",".join(map(str, cpus[2:4]))
    root = Path(tempfile.mkdtemp(prefix="small-ab-", dir=args.output.resolve()))
    print(f"Experiment: {root}", flush=True)
    source = binary.with_name("nginx.conf").read_text()
    # AB/BA/AB reduces simple order bias; each condition gets three measured rounds.
    results = [run(binary, source, root, i, enabled, server_cpus, client_cpus)
               for i, enabled in enumerate((False, True, True, False, False, True), 1)]
    summary = {"workload": {"concurrency": 20, "processes": 2, "requests_per_client": 3000,
                            "server_workers": 2, "worker_threads": 4, "server_cpus": server_cpus,
                            "client_cpus": client_cpus, "redis_lua": True, "warmup_per_run": 10000},
               "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "runs": results}
    for enabled, label in ((False, "off"), (True, "on")):
        group = [r for r in results if r["enabled"] == enabled]
        summary[label] = {"median_qps": statistics.median(r["qps"] for r in group),
                          "median_sampled_p95_ms": statistics.median(r["latency_ms"]["p95"] for r in group)}
    summary["qps_change_percent"] = (summary["on"]["median_qps"] / summary["off"]["median_qps"] - 1) * 100
    summary["p95_change_percent"] = (summary["on"]["median_sampled_p95_ms"] / summary["off"]["median_sampled_p95_ms"] - 1) * 100
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: summary[k] for k in ("off", "on", "qps_change_percent", "p95_change_percent")}, indent=2))


if __name__ == "__main__":
    main()
