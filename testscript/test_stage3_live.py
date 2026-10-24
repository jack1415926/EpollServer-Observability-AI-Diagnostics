#!/usr/bin/env python3
"""Real MySQL persistence, experiment comparisons, and optional retained browser QA instance."""
import argparse
import concurrent.futures
import json
import os
import pwd
from pathlib import Path
import secrets
import signal
import subprocess
import shutil
import tempfile
import time

from benchmark_worker_snapshot import configuration
from import_experiment import upload
from protocol import require
from run_monitor_experiments import run_experiments
from test_http_monitor import TOKEN, free_port, request, wait_for
from test_shutdown import children

ROOT = Path(__file__).resolve().parents[1]


class TestDatabase:
    """A private MySQL instance; never grants privileges on the business database."""
    def __init__(self, root):
        self.evidence = root
        # Ubuntu's shipped MySQL AppArmor profile permits owner files under /tmp.
        self.root = Path(tempfile.mkdtemp(prefix="epoll-monitor-mysql-"))
        self.process = None

    def __enter__(self):
        self.evidence.mkdir(mode=0o700)
        data = self.root / "data"
        user = pwd.getpwuid(os.geteuid()).pw_name
        result = subprocess.run(["mysqld", "--no-defaults", f"--user={user}", "--initialize-insecure", f"--datadir={data}",
                                 f"--log-error={self.root / 'init.log'}"], capture_output=True, timeout=90)
        (self.root / "init-console.log").write_bytes(result.stdout + result.stderr)
        if result.returncode != 0:
            self.__exit__(None, None, None)
            raise ValueError(f"private MySQL initialization failed; see {self.evidence}")
        port = free_port()
        self.socket = self.root / "mysql.sock"
        self.process = subprocess.Popen(["mysqld", "--no-defaults", f"--user={user}", f"--datadir={data}", "--bind-address=127.0.0.1",
                                         f"--port={port}", f"--socket={self.socket}", f"--pid-file={self.root / 'mysql.pid'}",
                                         f"--log-error={self.root / 'mysql.log'}", "--mysqlx=0", "--skip-log-bin",
                                         "--innodb-buffer-pool-size=32M", "--performance-schema=OFF", "--secure-file-priv=NULL"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        command = ["mysql", "--protocol=socket", "--connect-timeout=2", "-S", str(self.socket), "-u", "root", "--skip-password"]
        self.admin = command
        def ready():
            require(self.process.poll() is None, f"private MySQL exited; see {self.root / 'mysql.log'}")
            result = subprocess.run([*command, "-Nse", "SELECT 1"], capture_output=True, timeout=5)
            return result.returncode == 0
        try:
            wait_for(ready, timeout=30)
            password = secrets.token_hex(24)
            sql = "CREATE DATABASE epoll_monitor_test CHARACTER SET utf8mb4; USE epoll_monitor_test;\n" + (ROOT / "sql/monitor.sql").read_text()
            sql += f"\nCREATE USER 'monitor_test'@'127.0.0.1' IDENTIFIED BY '{password}'; GRANT SELECT,INSERT ON epoll_monitor_test.monitor_experiment TO 'monitor_test'@'127.0.0.1';"
            result = subprocess.run(command, input=sql, capture_output=True, text=True, timeout=10)
            require(result.returncode == 0, "private monitor schema/account initialization failed")
            environment = {**os.environ, "EPOLL_MONITOR_TOKEN": TOKEN, "EPOLL_MONITOR_DB_HOST": "127.0.0.1",
                    "EPOLL_MONITOR_DB_PORT": str(port), "EPOLL_MONITOR_DB_NAME": "epoll_monitor_test",
                    "EPOLL_MONITOR_DB_USER": "monitor_test", "EPOLL_MONITOR_DB_PASSWORD": password}
            architecture = subprocess.check_output(["g++", "-dumpmachine"], text=True).strip()
            plugins = ROOT / f".deps/usr/lib/{architecture}/libmariadb3/plugin"
            if plugins.is_dir(): environment["MARIADB_PLUGIN_DIR"] = str(plugins)
            return environment
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.process is not None and self.process.poll() is None:
            # Ubuntu AppArmor can deny signals to mysqld even from the same UID.
            subprocess.run([*self.admin, "--execute=SHUTDOWN"], capture_output=True, timeout=10, check=True)
            self.process.wait(timeout=30)
        for name in ("init.log", "init-console.log", "mysql.log"):
            if (self.root / name).exists(): shutil.copy2(self.root / name, self.evidence / name)
        shutil.rmtree(self.root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold", action="store_true", help="wait for stdin newline before cleaning up browser QA servers")
    parser.add_argument("--reports", type=Path, help="reuse a previously generated experiment evidence directory")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="stage3-live-", dir=ROOT / "artifacts"))
    print(f"Evidence: {root}", flush=True)
    database = TestDatabase(root / "database")
    environment = database.__enter__()
    tcp = monitor = None
    workers = []
    config = root / "nginx.conf"
    try:
        reports = sorted(args.reports.glob("round-*-experiment.json")) if args.reports else run_experiments(ROOT / "server/nginx", root / "experiments")
        require(len(reports) == 6, "expected six real measured runs")
        tcp_port, http_port = free_port(), free_port()
        config = root / "nginx.conf"
        config.write_text(configuration((ROOT / "server/nginx.conf").read_text(), tcp_port, True))
        config.chmod(0o600)
        with (root / "tcp-console.log").open("wb") as tcp_log, (root / "http-console.log").open("wb") as http_log:
            tcp = subprocess.Popen([str(ROOT / "server/nginx")], cwd=root, stdout=tcp_log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
            def ready():
                require(tcp.poll() is None, "TCP exited during startup")
                return len(list((root / "metrics").glob("worker-*.json"))) == 2
            wait_for(ready, timeout=30)
            workers = children(tcp.pid)
            manifest = next((root / "metrics").glob("master-*.json"))
            def start(env):
                return subprocess.Popen([str(ROOT / "monitor/build/epoll-monitor"), str(manifest), str(http_port)],
                                        cwd=root, env=env, stdout=http_log, stderr=subprocess.STDOUT)
            def monitor_ready():
                require(monitor.poll() is None, "monitor exited during startup")
                try:
                    return request(http_port)[1]["validity"] == "complete"
                except OSError:
                    return False
            monitor = start(environment)
            wait_for(monitor_ready)
            wait_for(lambda: request(http_port, "experiments")[0] == 200)
            ids = [upload(f"http://127.0.0.1:{http_port}", json.loads(path.read_text()), TOKEN) for path in reports]
            require(all(len(key) == 36 for key in ids), "invalid imported IDs")
            for key, report_file in zip(ids, reports):
                stored = request(http_port, f"experiments/{key}")
                require(stored[0] == 200 and stored[1]["data"]["report"] == json.loads(report_file.read_text()), "stored report differs from input")
            print("PASS: six measured reports imported and read back exactly", flush=True)

            def compare(a, b):
                return request(http_port, "experiments/compare", method="POST", body=json.dumps({"baseline": a, "candidate": b}))
            code, comparison, _ = compare(ids[0], ids[1])
            require(code == 200 and not comparison["data"]["same_conditions"], "different concurrency treated as identical")
            require("/benchmark/load" in comparison["data"]["condition_differences"], "load difference missing")
            a, b = [json.loads(path.read_text())["benchmark"]["result"]["qps"] for path in reports[:2]]
            require(abs(comparison["data"]["metrics"]["qps"]["change_percent"] - (b-a)/a*100) < 1e-8, "QPS percentage calculation wrong")
            require(comparison["data"]["metrics"]["unsuccessful_percent"]["change_percent"] is None, "zero baseline became infinite percent")
            require(compare(ids[0], ids[0])[1]["data"]["same_conditions"], "same report conditions differ")
            require(compare(ids[0], "0" * 8 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 12)[0] == 404, "missing experiment not rejected")
            (root / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
            print("PASS: real comparisons, condition differences, zero baseline, missing ID", flush=True)
            monitor.terminate()
            require(monitor.wait(timeout=10) == 0, "monitor shutdown failed")
            monitor = start(environment)
            wait_for(monitor_ready)
            wait_for(lambda: request(http_port, f"experiments/{ids[0]}")[0] == 200)
            print("PASS: MySQL records survive HTTP service restart", flush=True)

            monitor.terminate(); monitor.wait(timeout=10)
            bad_env = {**environment, "EPOLL_MONITOR_DB_PORT": str(free_port())}
            monitor = start(bad_env)
            wait_for(monitor_ready)
            started = time.monotonic()
            require(request(http_port, "experiments")[0] == 503, "broken database did not degrade")
            require(time.monotonic() - started < 4, "DB timeout not bounded")
            require(request(http_port)[1]["validity"] == "complete", "database failure affected live metrics")
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                pending = [pool.submit(request, http_port, "experiments") for _ in range(16)]
                started = time.monotonic()
                require(request(http_port)[1]["validity"] == "complete" and time.monotonic() - started < 1,
                        "queued database queries blocked live metrics")
                failures = [future.result() for future in pending]
            require(all(value[0] == 503 for value in failures) and
                    any(value[1]["code"] == "experiment_store_busy" for value in failures), "database admission limit not enforced")
            print("PASS: database failure returns bounded 503; live metrics remain complete", flush=True)
            monitor.terminate(); monitor.wait(timeout=10)
            monitor = start(environment)
            wait_for(monitor_ready)
            (root / "browser.json").write_text(json.dumps({"url": f"http://127.0.0.1:{http_port}", "tcp_port": tcp_port, "worker_pids": workers, "ids": ids, "reports": [str(path) for path in reports]}, indent=2) + "\n")
            print(f"Browser QA: http://127.0.0.1:{http_port}; metadata: {root / 'browser.json'}", flush=True)
            if args.hold:
                input("Press Enter to clean up QA servers.\n")
    finally:
        if monitor is not None and monitor.poll() is None:
            monitor.terminate()
            try: monitor.wait(timeout=10)
            except subprocess.TimeoutExpired: monitor.kill(); monitor.wait(timeout=5)
        if tcp is not None:
            if tcp.poll() is None:
                tcp.terminate()
                try: tcp.wait(timeout=30)
                except subprocess.TimeoutExpired: os.killpg(tcp.pid, signal.SIGKILL)
            if any(Path(f"/proc/{pid}").exists() for pid in workers):
                try: os.killpg(tcp.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            tcp.wait(timeout=5)
        config.unlink(missing_ok=True)
        database.__exit__(None, None, None)
        print("CLEANUP: private MySQL exited before its directory was removed; TCP/HTTP processes stopped", flush=True)


if __name__ == "__main__":
    main()
