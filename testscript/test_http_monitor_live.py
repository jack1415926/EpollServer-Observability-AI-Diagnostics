#!/usr/bin/env python3
"""Drogon/TCP fault isolation acceptance; uses existing local MySQL and Redis."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from process_group import start_process_group, stop_process_group
from protocol import recv_pkg, require, send_pkg
from test_shutdown import children, configure, connect
from test_http_monitor import TOKEN, free_port, request, wait_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path("server/nginx"))
    parser.add_argument("--monitor", type=Path, default=Path("monitor/build/epoll-monitor"))
    args = parser.parse_args()
    binary, monitor_binary = args.binary.resolve(), args.monitor.resolve()
    root = Path(tempfile.mkdtemp(prefix="epoll-http-live-"))
    tcp_port, http_port = free_port(), free_port()
    config = root / "nginx.conf"
    metrics = root / "metrics"
    source = (binary.parent / "nginx.conf").read_text()
    source = "\n".join(line for line in configure(source, tcp_port).splitlines()
                       if not line.strip().startswith(("WorkerSnapshotEnable", "WorkerSnapshotDirectory")))
    config.write_text(source + f"\nWorkerSnapshotEnable = 1\nWorkerSnapshotDirectory = {metrics}\n")
    tcp = monitor = None
    workers = []
    try:
        with (root / "tcp-console.log").open("wb") as tcp_log, (root / "http-console.log").open("wb") as http_log:
            tcp = start_process_group([str(binary)], cwd=root, stdout=tcp_log, stderr=subprocess.STDOUT)
            def ready():
                require(tcp.poll() is None, "TCP exited during startup")
                log = root / "server.log"
                return (log.exists() and log.read_text(errors="replace").count("【worker进程】启动") == 2
                        and len(list(metrics.glob("master-*.json"))) == 1)
            wait_for(ready, timeout=30)
            workers = children(tcp.pid)
            manifest = next(metrics.glob("master-*.json"))
            def start_monitor():
                return subprocess.Popen([str(monitor_binary), str(manifest), str(http_port)], cwd=root,
                                        env={**os.environ, "EPOLL_MONITOR_TOKEN": TOKEN},
                                        stdout=http_log, stderr=subprocess.STDOUT)
            def complete():
                require(monitor.poll() is None, "HTTP exited during startup")
                try:
                    return request(http_port)[1]["validity"] == "complete"
                except OSError:
                    return False
            def save(name, endpoint="overview"):
                status, result, _ = request(http_port, endpoint)
                (root / f"{name}.json").write_text(json.dumps({"http_status": status, **result}, indent=2) + "\n")
                return result
            def ping(source_ip):
                with connect(tcp_port, source_ip) as client:
                    send_pkg(client, 0, b"")
                    require(recv_pkg(client) == (0, b""), "TCP Ping failed")

            monitor = start_monitor()
            wait_for(complete)
            require(save("complete")["data"]["totals"]["connections_current"] == 0, "unexpected initial connections")
            with connect(tcp_port, "127.0.10.1") as client:
                send_pkg(client, 0, b"")
                require(recv_pkg(client) == (0, b""), "initial Ping failed")
                wait_for(lambda: request(http_port)[1]["data"]["totals"]["connections_current"] == 1)
                save("connection-one")
            wait_for(lambda: request(http_port)[1]["data"]["totals"]["connections_current"] == 0)
            print("PASS: HTTP reports live TCP connections 0 -> 1 -> 0", flush=True)

            os.kill(workers[0], signal.SIGSTOP)
            try:
                wait_for(lambda: request(http_port)[1]["validity"] == "partial", timeout=8)
                partial = save("partial")
                require(partial["data"]["valid_workers"] == 1 and
                        partial["data"]["totals"]["connections_current"] is None, "partial became fake total")
            finally:
                os.kill(workers[0], signal.SIGCONT)
            wait_for(complete)
            print("PASS: stopped Worker expires; resume recovers complete", flush=True)

            manifest.chmod(0o666)
            try:
                wait_for(lambda: request(http_port)[0] == 503)
                save("invalid-manifest")
                ping("127.0.10.2")
            finally:
                manifest.chmod(0o600)
            wait_for(complete)
            monitor.kill()
            monitor.wait(timeout=5)
            ping("127.0.10.3")
            print("PASS: invalid monitoring data and HTTP crash leave TCP Ping working", flush=True)

            monitor = start_monitor()
            wait_for(complete)
            os.kill(workers[0], signal.SIGKILL)
            require(tcp.wait(timeout=30) == 1, "TCP fail-stop behavior changed")
            wait_for(lambda: request(http_port)[0] == 503)
            save("tcp-exited")
            health = save("health-after-tcp-exit", "health")
            require(health["data"]["monitor"] == "ok" and health["data"]["tcp_health"] == "not_probed",
                    "HTTP health confused with TCP health")
            monitor.terminate()
            require(monitor.wait(timeout=10) == 0, "HTTP did not stop cleanly")
            print("PASS: TCP Worker crash retains fail-stop; HTTP remains available", flush=True)
            print(f"Evidence: {root}", flush=True)
    finally:
        def stop_tcp():
            if tcp is not None:
                stop_process_group(tcp)
            config.unlink(missing_ok=True)

        def stop_monitor():
            if monitor is not None and monitor.poll() is None:
                monitor.kill()
                monitor.wait(timeout=5)

        with ExitStack() as cleanup:
            cleanup.callback(stop_tcp)
            cleanup.callback(stop_monitor)


if __name__ == "__main__":
    main()
