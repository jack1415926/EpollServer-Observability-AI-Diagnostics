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
import select
import sys

from process_group import start_process_group, stop_process_group
from protocol import recv_pkg, require, send_pkg
from test_shutdown import children, configure, connect
from test_http_monitor import TOKEN, free_port, request, wait_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path("server/nginx"))
    parser.add_argument("--monitor", type=Path, default=Path("monitor/build/epoll-monitor"))
    parser.add_argument("--resources", action="store_true", help="verify independent CPU/RSS; Ping-only profile disables Redis rate limiting in the private config")
    parser.add_argument("--tcp-probe", action="store_true", help="verify background Ping, timeout and recovery against this private TCP instance")
    parser.add_argument("--output", type=Path, help="new evidence directory; existing paths are rejected")
    parser.add_argument("--hold", action="store_true", help="hold resumed services for browser QA, at most 300 seconds")
    args = parser.parse_args()
    binary, monitor_binary = args.binary.resolve(), args.monitor.resolve()
    if args.output:
        root = args.output.resolve(); root.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        root = Path(tempfile.mkdtemp(prefix="epoll-http-live-"))
    tcp_port, http_port = free_port(), free_port()
    config = root / "nginx.conf"
    metrics = root / "metrics"
    source = (binary.parent / "nginx.conf").read_text()
    source = "\n".join(line for line in configure(source, tcp_port).splitlines()
                       if not line.strip().startswith(("WorkerSnapshotEnable", "WorkerSnapshotDirectory")))
    if args.resources:
        source = '\n'.join(line for line in source.splitlines() if not line.strip().startswith('RedisRateLimitEnable'))
        source += '\nRedisRateLimitEnable = 0\n'
    config.write_text(source + f"\nWorkerSnapshotEnable = 1\nWorkerSnapshotDirectory = {metrics}\n")
    config.chmod(0o600)
    (root/'parameters.json').write_text(json.dumps({'tcp_port':tcp_port,'http_port':http_port,
        'resources':args.resources,'tcp_probe':args.tcp_probe,'redis_rate_limit_enabled':False if args.resources else 'from_local_config',
        'scope':'functional Ping and resource observation, not a performance measurement'})+'\n')
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
                environment = {k:v for k,v in os.environ.items() if not k.startswith('EPOLL_MONITOR_TCP_')}
                environment['EPOLL_MONITOR_TOKEN'] = TOKEN
                if args.tcp_probe:
                    environment.update(EPOLL_MONITOR_TCP_PORT=str(tcp_port), EPOLL_MONITOR_TCP_INTERVAL_MS='500',
                                       EPOLL_MONITOR_TCP_TIMEOUT_MS='200')
                return subprocess.Popen([str(monitor_binary), str(manifest), str(http_port)], cwd=root,
                                        env=environment,
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
            initial = save("complete")
            if not args.tcp_probe:
                require(initial["data"]["totals"]["connections_current"] == 0, "unexpected initial connections")
            if args.resources:
                wait_for(lambda: all(w['resources']['cpu_percent'] is not None for w in request(http_port,'workers')[1]['data']['workers']))
                resource = save('resources-idle', 'workers')
                require(all(w['resources']['rss_bytes'] > 0 for w in resource['data']['workers']), 'missing Worker RSS')
            with connect(tcp_port, "127.0.10.1") as client:
                send_pkg(client, 0, b"")
                require(recv_pkg(client) == (0, b""), "initial Ping failed")
                wait_for(lambda: request(http_port)[1]["data"]["totals"]["connections_current"] >= 1)
                save("connection-one")
            wait_for(lambda: request(http_port)[1]["data"]["totals"]["connections_current"] == 0)
            print("PASS: HTTP reports live TCP connections 0 -> 1 -> 0", flush=True)

            if args.tcp_probe:
                def probe_reason(reason):
                    return request(http_port, 'health')[1]['data']['tcp_probe']['reason'] == reason
                wait_for(lambda: probe_reason('ping_ok'))
                save('probe-success', 'health')
                for worker in workers: os.kill(worker, signal.SIGSTOP)
                try:
                    wait_for(lambda: probe_reason('timeout'))
                    failed = save('probe-timeout', 'health')
                    require(failed['data']['monitor']=='ok' and failed['data']['tcp_health']=='unreachable',
                            'probe timeout did not retain monitor liveness')
                finally:
                    for worker in workers: os.kill(worker, signal.SIGCONT)
                wait_for(lambda: probe_reason('ping_ok'))
                save('probe-recovered', 'health')
                print('PASS: real TCP probe success -> stopped Workers timeout -> resumed success', flush=True)

            os.kill(workers[0], signal.SIGSTOP)
            try:
                wait_for(lambda: request(http_port)[1]["validity"] == "partial", timeout=8)
                partial = save("partial")
                require(partial["data"]["valid_workers"] == 1 and
                        partial["data"]["totals"]["connections_current"] is None, "partial became fake total")
                if args.resources:
                    resource = save('resources-paused', 'workers')
                    paused = next(w for w in resource['data']['workers'] if w['pid']==workers[0])
                    require(paused['status']=='stale' and paused['resources']['rss_bytes'] > 0 and
                            paused['resources']['cpu_percent']==0, 'paused resources confused with stale business snapshot')
            finally:
                os.kill(workers[0], signal.SIGCONT)
            wait_for(complete)
            if args.resources: save('resources-resumed', 'workers')
            print("PASS: stopped Worker expires; resume recovers complete", flush=True)
            if args.hold:
                print(f'HOLD: HTTP port {http_port}; press Enter to finish, automatic continuation after 300s',flush=True)
                if select.select([sys.stdin], [], [], 300)[0]: sys.stdin.readline()

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
            if args.resources: save('resources-tcp-exited', 'workers')
            health = save("health-after-tcp-exit", "health")
            require(health["data"]["monitor"] == "ok" and health["data"]["tcp_health"] == ("unknown" if args.tcp_probe else "not_probed"),
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
