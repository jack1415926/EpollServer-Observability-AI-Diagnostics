#!/usr/bin/env python3
"""Two concurrency conditions, three alternating measured rounds each; isolated TCP instance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import tempfile
import uuid

from benchmark_worker_snapshot import configuration
from import_experiment import prepare_report
from protocol import require
from test_http_monitor import free_port, wait_for
from process_group import run_process_group, start_process_group, stop_process_group

ROOT = Path(__file__).resolve().parents[1]


def run_experiments(binary, root, build_mode="unknown"):
    root.mkdir(parents=True, exist_ok=True)
    cpus = sorted(os.sched_getaffinity(0))
    require(len(cpus) >= 4, "four available CPUs required")
    server_cpus, client_cpus = ",".join(map(str, cpus[:2])), ",".join(map(str, cpus[2:4]))
    port = free_port()
    config = root / "nginx.conf"
    config.write_text(configuration(binary.with_name("nginx.conf").read_text(), port, True))
    config.chmod(0o600)
    proc, reports = None, []
    try:
        with (root / "console.log").open("wb") as output:
            proc = start_process_group(["taskset", "-c", server_cpus, str(binary)], cwd=root,
                                    stdout=output, stderr=subprocess.STDOUT)
            def ready():
                require(proc.poll() is None, "isolated TCP server exited")
                log = root / "server.log"
                return log.exists() and log.read_text(errors="replace").count("【worker进程】启动") == 2
            wait_for(ready, timeout=30)
            sequence = [("warmup", 10, 500), *[(f"round-{i}", c, 2000) for i, c in enumerate((10, 20, 20, 10, 10, 20), 1)]]
            environment = {"system": platform.platform(), "machine": Path("/etc/machine-id").read_text().strip(),
                           "cpu": Path("/proc/cpuinfo").read_text().split("model name", 1)[-1].splitlines()[0], "affinity": cpus}
            context = {"environment_id": hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest(),
                       "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                       "client_sha256": hashlib.sha256((ROOT / "testscript/tcp_stress_test.py").read_bytes()).hexdigest(),
                       "server_cpus": server_cpus, "client_cpus": client_cpus, "snapshot_enabled": True,
                       "redis_window_sec": 60, "redis_max_requests": 100000000}
            for label, concurrency, requests in sequence:
                run_label = f"{label}-{uuid.uuid4().hex}"
                result_file, resource_file = root / f"{label}-result.json", root / f"{label}-resources.json"
                load = ["taskset", "-c", client_cpus, sys.executable, str(ROOT / "testscript/tcp_stress_test.py"),
                        "--port", str(port), "--timeout", "5", "--concurrency", str(concurrency), "--processes", "2",
                        "--requests-per-client", str(requests), "--source-ip", "127.0.15.1", "--latency-mode", "all",
                        "--label", run_label, "--build-mode", build_mode, "--server-workers", "2", "--worker-threads", "4",
                        "--rate-limit-mode", "enabled", "--json-out", str(result_file)]
                command = [sys.executable, str(ROOT / "testscript/performance_sampler.py"), "--master-pid", str(proc.pid),
                           "--label", run_label, "--interval", "0.2", "--output", str(resource_file), "--", *load]
                with (root / f"{label}.log").open("wb") as log:
                    run_process_group(command, stdout=log, stderr=subprocess.STDOUT, timeout=120)
                result = json.loads(result_file.read_text())["result"]
                require(result["failed"] == 0 and result["not_attempted"] == 0 and
                        result["successful"] == concurrency * requests, "experiment contains request failures")
                if label == "warmup":
                    continue
                context_file = root / f"{label}-context.json"
                current = {**context, "command": shlex.join(load)}
                context_file.write_text(json.dumps(current, indent=2) + "\n")
                report = prepare_report(f"并发 {concurrency} · {label}", result_file, resource_file, current)
                report_file = root / f"{label}-experiment.json"
                report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                reports.append(report_file)
                print(f"PASS {label}: concurrency={concurrency}, requests={result['successful']}, QPS={result['qps']}, P95={result['latency_ms']['p95']} ms", flush=True)
            return reports
    finally:
        if proc is not None:
            stop_process_group(proc)
        config.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "server/nginx")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--build-mode", choices=("debug", "release", "unknown"), default="unknown")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="stage3-experiments-", dir=args.output.resolve()))
    print(f"Evidence: {root}", flush=True)
    run_experiments(args.binary.resolve(strict=True), root, args.build_mode)


if __name__ == "__main__":
    main()
