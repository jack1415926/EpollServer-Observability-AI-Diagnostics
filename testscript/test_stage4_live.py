#!/usr/bin/env python3
"""Private MySQL + real /proc identities + controlled snapshots; no performance load."""
import argparse
from contextlib import ExitStack
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from protocol import require, send_pkg, recv_pkg
from benchmark_worker_snapshot import configuration
from process_group import start_process_group, stop_process_group
from test_shutdown import children, connect
from test_http_monitor import TOKEN, free_port, identity, request, wait_for, HttpMonitorTests
from test_stage3_live import TestDatabase

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="one actual trigger, SQL readback and recovery only")
    parser.add_argument("--hold", action="store_true", help="retain live services until stdin newline for browser QA")
    args = parser.parse_args()
    evidence = Path(tempfile.mkdtemp(prefix="stage4-live-", dir=ROOT / "artifacts"))
    evidence.chmod(0o700)
    print(f"Evidence: {evidence}", flush=True)
    with ExitStack() as cleanup:
        database = TestDatabase(evidence / "database")
        environment = database.__enter__()
        cleanup.callback(database.__exit__, None, None, None)

        def sql(statement):
            return subprocess.run([*database.admin, "-Nse", statement], capture_output=True,
                                  text=True, check=True, timeout=10).stdout

        grant = "GRANT SELECT,INSERT,UPDATE ON epoll_monitor_test.monitor_event TO 'monitor_test'@'127.0.0.1'"
        sql(grant)
        directory = evidence / "snapshots"
        directory.mkdir(mode=0o700)
        worker = subprocess.Popen(["sleep", "3600"])

        def stop_worker():
            if worker.poll() is None: worker.terminate()
            worker.wait(timeout=5)

        cleanup.callback(stop_worker)
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        ticks = identity(os.getpid())
        member = {"pid": worker.pid, "process_start_ticks": identity(worker.pid), "worker_index": 0}
        manifest = directory / f"master-{boot}-{os.getpid()}-{ticks}.json"
        master = {"schema_version": 1, "boot_id": boot, "master_pid": os.getpid(), "master_start_ticks": ticks,
                  "sequence": 1, "write_failures_total": 0, "state": "running", "expected_workers": 1, "workers": [member]}
        HttpMonitorTests.write(manifest, master)
        snapshot = directory / f"worker-{boot}-{worker.pid}-{member['process_start_ticks']}.json"
        values = {"receive_queue_messages": 0, "send_queue_messages": 0}
        publishing = threading.Event()
        publishing.set()
        stopped = threading.Event()

        def publish():
            sequence = 0
            while not stopped.is_set():
                if publishing.is_set():
                    sequence += 1
                    HttpMonitorTests.write(snapshot, {**member, **values, "schema_version": 1, "boot_id": boot,
                        "master_pid": os.getpid(), "master_start_ticks": ticks, "sequence": sequence,
                        "write_failures_total": 0, "collection_ok": True, "connections_current": 0,
                        "sampled_at_unix_ms": int(time.time() * 1000),
                        "sampled_at_boottime_ms": int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000)})
                stopped.wait(0.1)

        publisher = threading.Thread(target=publish)
        publisher.start()

        def stop_publisher():
            stopped.set(); publisher.join(timeout=5)
            require(not publisher.is_alive(), "snapshot publisher did not stop")

        cleanup.callback(stop_publisher)
        port = free_port()
        environment.update(EPOLL_MONITOR_EVENT_TRIGGER_MS="500", EPOLL_MONITOR_EVENT_RECOVERY_MS="500")
        log = cleanup.enter_context((evidence / "http.log").open("ab"))
        monitor = None
        current_instance = None

        def stop_monitor():
            if monitor is not None:
                if monitor.poll() is None: monitor.terminate()
                monitor.wait(timeout=10)

        cleanup.callback(stop_monitor)

        def start_monitor():
            nonlocal monitor, current_instance
            monitor = subprocess.Popen([str(ROOT / "monitor/build/epoll-monitor"), str(manifest), str(port)],
                env=environment, stdout=log, stderr=subprocess.STDOUT)

            def ready():
                require(monitor.poll() is None, f"monitor exited; see {evidence / 'http.log'}")
                try: return request(port, "events")[1].get("data", {}).get("detection", {}).get("ready")
                except (OSError, AttributeError): return False

            wait_for(ready, timeout=15)
            current_instance = request(port, "events")[1]["data"]["detection"]["instance"]

        def events():
            code, result, _ = request(port, "events")
            require(code == 200, f"event query failed: {result['code']}")
            return result["data"]["events"]

        def find(rule, status):
            return next((e for e in events() if e["instance"] == current_instance and e["rule"] == rule and e["status"] == status), None)

        start_monitor()
        require(events() == [], "unexpected initial events")
        values["receive_queue_messages"] = 1024
        active = wait_for(lambda: find("receive_queue_backlog", "active"))
        require(json.loads(sql(f"SELECT report FROM epoll_monitor_test.monitor_event WHERE id='{active['id']}'")) == active,
                "actual SQL event readback differs")
        values["receive_queue_messages"] = 0
        resolved = wait_for(lambda: find("receive_queue_backlog", "resolved"))
        require(resolved["id"] == active["id"], "recovery created a different incident")
        if not args.smoke:
            # Persistence / restart deduplication, interrupted recovery and both queue directions.
            values["receive_queue_messages"] = 1024
            second = wait_for(lambda: find("receive_queue_backlog", "active"))
            require(second["id"] != active["id"], "new occurrence reused an old ID")
            stop_monitor(); start_monitor()
            time.sleep(1.5)
            require(sum(e["rule"] == "receive_queue_backlog" for e in events()) == 2, "duplicate after restart")
            publishing.clear()
            wait_for(lambda: find("collection_unavailable", "active"))
            require(find("receive_queue_backlog", "active")["id"] == second["id"], "missing metrics falsely recovered queue")
            values["receive_queue_messages"] = 0
            publishing.set()
            wait_for(lambda: not find("receive_queue_backlog", "active") and not find("collection_unavailable", "active"))

            sql("REVOKE INSERT,UPDATE ON epoll_monitor_test.monitor_event FROM 'monitor_test'@'127.0.0.1'")
            values["send_queue_messages"] = 1024
            wait_for(lambda: request(port, "events")[1]["data"]["detection"]["store_status"] == "unavailable")
            require(request(port)[1]["validity"] == "complete", "SQL failure affected live sampling")
            sql(grant)
            wait_for(lambda: find("send_queue_backlog", "active"))
            values["send_queue_messages"] = 0
            wait_for(lambda: find("send_queue_backlog", "resolved"))
            stop_worker()
            wait_for(lambda: find("worker_process_anomaly", "active"))
            before = len(events())
            master["state"] = "stopping"
            HttpMonitorTests.write(manifest, master)
            time.sleep(2)
            require(len(events()) == before, "normal stopping generated new events")
            require(find("worker_process_anomaly", "active") is not None, "stopping falsely recovered dead worker")
            # Repeat collection trigger/recovery with an actual TCP Worker, without load generation.
            stop_monitor()
            tcp_root = evidence / "tcp"
            tcp_root.mkdir(mode=0o700)
            tcp_port = free_port()
            config = tcp_root / "nginx.conf"
            config.write_text(configuration((ROOT / "server/nginx.conf").read_text(), tcp_port, True))
            config.chmod(0o600)
            tcp_log = cleanup.enter_context((tcp_root / "console.log").open("wb"))
            tcp = start_process_group([str(ROOT / "server/nginx")], cwd=tcp_root, stdout=tcp_log, stderr=subprocess.STDOUT)

            def stop_tcp():
                stop_process_group(tcp)
                config.unlink(missing_ok=True)

            cleanup.callback(stop_tcp)

            def tcp_ready():
                require(tcp.poll() is None, f"TCP exited; see {tcp_root}")
                return len(list((tcp_root / "metrics").glob("worker-*.json"))) == 2

            wait_for(tcp_ready, timeout=30)
            manifest = next((tcp_root / "metrics").glob("master-*.json"))
            start_monitor()
            wait_for(lambda: request(port)[1]["validity"] == "complete")
            with connect(tcp_port, "127.0.10.40") as client:
                send_pkg(client, 0, b"")
                require(recv_pkg(client) == (0, b""), "actual TCP Ping failed")
            pid = children(tcp.pid)[0]
            start_ticks = identity(pid)
            require(identity(pid) == start_ticks, "TCP Worker identity changed")
            os.kill(pid, signal.SIGSTOP)
            try:
                wait_for(lambda: find("collection_unavailable", "active"), timeout=12)
                require(find("worker_process_anomaly", "active") is None, "paused Worker was reported as exited")
            finally:
                require(identity(pid) == start_ticks, "TCP Worker identity changed before resume")
                os.kill(pid, signal.SIGCONT)
            wait_for(lambda: find("collection_unavailable", "resolved"))
            wait_for(lambda: request(port)[1]["validity"] == "complete")
        for endpoint, token, method, body, expected in [
            ("events", None, "GET", None, 401), ("events", TOKEN, "POST", None, 405),
            ("events?limit=1000", TOKEN, "GET", None, 400), ("events", TOKEN, "GET", "{}", 400)]:
            require(request(port, endpoint, token, method, body)[0] == expected, "event request boundary failed")
        (evidence / "events.json").write_text(json.dumps(events(), indent=2))
        if args.hold:
            print(f"Browser QA: http://127.0.0.1:{port}/ ; token is test_http_monitor.TOKEN", flush=True)
            input("Press Enter to clean up: ")
    require(monitor.poll() is not None and worker.poll() is not None and not database.root.exists(), "cleanup incomplete")
    print("PASS: event persistence, recovery and resource cleanup" + (" (smoke)" if args.smoke else " (full)"), flush=True)


if __name__ == "__main__":
    main()
