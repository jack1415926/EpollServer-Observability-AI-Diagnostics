"""Real Drogon HTTP acceptance with real /proc identities and controlled snapshot files."""
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import copy
try:
    from .experiment_fixture import valid_report
except ImportError:
    from experiment_fixture import valid_report

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-monitor-token-0123456789abcdef"


def wait_for(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition timed out")


def identity(pid):
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, endpoint="overview", token=TOKEN, method="GET", body=None, raw=False):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        headers = {} if token is None else {"Authorization": "Bearer " + token}
        connection.request(method, "/api/v1/" + endpoint, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        return response.status, data if raw else json.loads(data), {k.lower(): v for k, v in response.getheaders()}
    finally:
        connection.close()


class HttpMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["make", "-C", str(ROOT / "monitor"), "http"], check=True, timeout=120)
        cls.binary = ROOT / "monitor/build/epoll-monitor"
        cls.build = tempfile.TemporaryDirectory(prefix="epoll-http-probe-")
        cls.addClassCleanup(cls.build.cleanup)
        source = Path(cls.build.name) / "slow.c"
        source.write_text('''#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int openat(int fd, const char *path, int flags, ...) {
    int (*real_openat)(int, const char *, int, ...) = dlsym(RTLD_NEXT, "openat");
    const char *trigger = getenv("EPOLL_TEST_BLOCK");
    if (trigger && !strncmp(path, "master-", 7) && access(trigger, F_OK) == 0) {
        unlink(trigger);
        const char *marker = getenv("EPOLL_TEST_ENTERED");
        int out = open(marker, O_CREAT | O_WRONLY, 0600);
        if (out >= 0) close(out);
        usleep(5000000);
    }
    return real_openat(fd, path, flags, 0600);
}
''')
        cls.preload = Path(cls.build.name) / "slow.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                        str(source), "-ldl", "-o", str(cls.preload)], check=True, timeout=30)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="epoll-http-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workers = [subprocess.Popen(["sleep", "60"]) for _ in range(2)]
        self.addCleanup(self.stop_workers)
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        ticks = identity(os.getpid())
        self.master = {"schema_version": 1, "boot_id": boot, "master_pid": os.getpid(),
                       "master_start_ticks": ticks, "sequence": 1, "write_failures_total": 0,
                       "state": "running", "expected_workers": 2,
                       "workers": [{"worker_index": i, "pid": w.pid, "process_start_ticks": identity(w.pid)}
                                   for i, w in enumerate(self.workers)]}
        self.manifest = self.root / f"master-{boot}-{os.getpid()}-{ticks}.json"
        self.write(self.manifest, self.master)
        self.files = []
        self.samples = []
        for row in self.master["workers"]:
            sample = {**row, "schema_version": 1, "boot_id": boot, "master_pid": os.getpid(),
                      "master_start_ticks": ticks, "sequence": 1, "write_failures_total": 0,
                      "collection_ok": True, "sampled_at_unix_ms": int(time.time() * 1000),
                      "sampled_at_boottime_ms": int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000),
                      "connections_current": row["worker_index"] + 1,
                      "receive_queue_messages": 4, "send_queue_messages": 5}
            path = self.root / f"worker-{boot}-{row['pid']}-{row['process_start_ticks']}.json"
            self.write(path, sample)
            self.files.append(path)
            self.samples.append(sample)
        self.port = free_port()
        self.log = (self.root / "monitor.log").open("wb")
        self.addCleanup(self.log.close)
        self.monitor = subprocess.Popen([str(self.binary), str(self.manifest), str(self.port)],
                                        cwd=self.root, stdout=self.log, stderr=subprocess.STDOUT,
                                        env={**os.environ, "EPOLL_MONITOR_TOKEN": TOKEN,
                                             "LD_PRELOAD": str(self.preload),
                                             "EPOLL_TEST_BLOCK": str(self.root / "block"),
                                             "EPOLL_TEST_ENTERED": str(self.root / "entered")})
        self.addCleanup(self.stop_monitor)
        def ready():
            self.assertIsNone(self.monitor.poll(), self.root.joinpath("monitor.log").read_text())
            try:
                return request(self.port)[1]["validity"] == "complete"
            except OSError:
                return False
        wait_for(ready)

    def stop_workers(self):
        for worker in self.workers:
            if worker.poll() is None:
                worker.terminate()
            worker.wait(timeout=5)

    def stop_monitor(self):
        if self.monitor.poll() is None:
            self.monitor.terminate()
        self.monitor.wait(timeout=10)

    @staticmethod
    def write(path, value):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        temporary.chmod(0o600)
        temporary.replace(path)

    def test_four_endpoints_and_real_totals(self):
        for endpoint in ("health", "overview", "workers", "metrics"):
            status, result, headers = request(self.port, endpoint)
            self.assertEqual(status, 200)
            self.assertTrue(result["data_valid"])
            self.assertEqual(result["validity"], "complete")
            self.assertIsInstance(result["timestamp_unix_ms"], int)
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertEqual(result["data"]["tcp_health"], "not_probed")
            if endpoint in ("overview", "metrics"):
                self.assertEqual(result["data"]["totals"]["connections_current"], 3)
                self.assertIn("cpu_memory", result["data"]["unsupported_metrics"])
            if endpoint == "workers":
                self.assertEqual(len(result["data"]["workers"]), 2)
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_auth_input_and_no_static_files(self):
        (self.root / "secret.txt").write_text("DO-NOT-EXPOSE")
        for endpoint, token, method, body, expected in [
            ("health", None, "GET", None, 401),
            ("health", "wrong", "GET", None, 401),
            ("../secret.txt", TOKEN, "GET", None, 404),
            ("events", TOKEN, "GET", None, 404),
            ("overview", TOKEN, "POST", None, 405),
            ("overview?manifest=elsewhere", TOKEN, "GET", None, 400),
            ("overview", TOKEN, "GET", "{}", 400),
        ]:
            with self.subTest(endpoint=endpoint, expected=expected):
                status, value, _ = request(self.port, endpoint, token, method, body)
                self.assertEqual(status, expected)
                self.assertFalse(value["data_valid"])
                self.assertNotIn(TOKEN, json.dumps(value))
                self.assertNotIn("DO-NOT-EXPOSE", json.dumps(value))
        # Drogon's parser rejects oversized bodies before application advice runs.
        self.assertEqual(request(self.port, method="POST", body="x" * 2048, raw=True)[0], 413)

    def test_partial_corrupt_and_recovery(self):
        self.files[0].write_text("{")
        wait_for(lambda: request(self.port)[1]["validity"] == "partial")
        status, result, _ = request(self.port)
        self.assertEqual(status, 200)
        self.assertFalse(result["data_valid"])
        self.assertIsNone(result["data"]["totals"]["connections_current"])
        self.assertEqual(result["data"]["partial_sums"]["connections_current"], 2)
        self.write(self.files[0], self.samples[0])
        wait_for(lambda: request(self.port)[1]["validity"] == "complete")

    def test_stale_and_unavailable_health_stays_alive(self):
        for path, sample in zip(self.files, self.samples):
            self.write(path, {**sample, "sampled_at_boottime_ms": sample["sampled_at_boottime_ms"] - 5000})
        wait_for(lambda: request(self.port)[0] == 503)
        status, result, _ = request(self.port, "workers")
        self.assertEqual(status, 503)
        self.assertTrue(all(w["status"] == "stale" for w in result["data"]["workers"]))
        self.manifest.unlink()
        wait_for(lambda: request(self.port)[1]["code"] == "missing")
        status, health, _ = request(self.port, "health")
        self.assertEqual(status, 200)
        self.assertEqual(health["data"]["monitor"], "ok")
        self.assertEqual(health["data"]["collection_status"], "unavailable")

    def test_stopping_and_dead_identity(self):
        self.write(self.manifest, {**self.master, "state": "stopping"})
        wait_for(lambda: request(self.port)[1]["validity"] == "stopping")
        self.write(self.manifest, self.master)
        self.workers[0].kill()
        self.workers[0].wait(timeout=5)
        wait_for(lambda: request(self.port, "workers")[1]["data"]["workers"][0]["status"] == "dead")
        self.assertIsNone(request(self.port)[1]["data"]["totals"]["connections_current"])

    def test_slow_collection_keeps_http_responsive_and_expires_cache(self):
        (self.root / "block").touch()
        wait_for(lambda: (self.root / "entered").exists())
        wait_for(lambda: request(self.port)[1]["code"] == "collection_timeout", timeout=4)
        started = time.monotonic()
        status, value, _ = request(self.port, "health")
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(status, 200)
        self.assertEqual(value["code"], "collection_timeout")
        def recovered():
            for path, sample in zip(self.files, self.samples):
                sample["sampled_at_boottime_ms"] = int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000)
                self.write(path, sample)
            return request(self.port)[1]["validity"] == "complete"
        wait_for(recovered, timeout=8)

    def test_global_rate_limit_and_recovery(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            statuses = list(pool.map(lambda _: request(self.port, "health")[0], range(200)))
        self.assertIn(429, statuses)
        self.assertTrue(set(statuses) <= {200, 429})
        time.sleep(0.1)
        self.assertEqual(request(self.port, "health")[0], 200)

    def test_startup_validation(self):
        for args, token in [([], TOKEN), ([str(self.manifest)], "short"),
                            ([str(self.manifest)], TOKEN + "\n"),
                            (["relative.json"], TOKEN), ([str(self.manifest), "0"], TOKEN),
                            ([str(self.manifest), "65536"], TOKEN)]:
            run = subprocess.run([str(self.binary), *args], env={**os.environ, "EPOLL_MONITOR_TOKEN": token},
                                 capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 64)
            self.assertNotIn(TOKEN, run.stderr)
        self.assertEqual(subprocess.run([str(self.binary), "--help"], capture_output=True).returncode, 0)
        for invalid in ({"EPOLL_MONITOR_DB_NAME": "test db"},
                        {"EPOLL_MONITOR_DB_NAME": "test", "EPOLL_MONITOR_DB_USER": "bad user"},
                        {"EPOLL_MONITOR_DB_NAME": "test", "EPOLL_MONITOR_DB_PASSWORD": "bad\npassword"}):
            run = subprocess.run([str(self.binary), str(self.manifest)],
                                 env={**os.environ, "EPOLL_MONITOR_TOKEN": TOKEN, **invalid},
                                 capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 64)
            self.assertNotIn("bad", run.stderr)

    def test_public_dashboard_assets_and_api_auth(self):
        for path in ("/", "/app.js", "/style.css"):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 100)
                self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
            finally:
                connection.close()
        self.assertEqual(request(self.port, "experiments", token=None)[0], 401)
        self.assertEqual(request(self.port, "experiments")[1]["code"], "experiment_store_disabled")

    def test_experiment_import_validation_and_body_limits(self):
        fixture = valid_report()
        self.assertEqual(request(self.port, "experiments", method="POST", body=json.dumps(fixture))[1]["code"], "experiment_store_disabled")
        fixture["benchmark"]["git_commit"] = "f03f3f3"
        self.assertEqual(request(self.port, "experiments", method="POST", body=json.dumps(fixture))[1]["code"], "experiment_store_disabled")
        cases = ["{", '{"name":"a","name":"b"}', '[' * 25 + '0' + ']' * 25]
        for path, value in [("name", "x" * 129), ("unexpected_config", {"password": "do-not-store"})]:
            invalid = copy.deepcopy(fixture)
            invalid[path] = value
            cases.append(json.dumps(invalid))
        for key, value in [("failed", -1), ("successful", True), ("attempted", 11), ("qps", "10")]:
            invalid = copy.deepcopy(fixture)
            invalid["benchmark"]["result"][key] = value
            cases.append(json.dumps(invalid))
        invalid = copy.deepcopy(fixture)
        invalid["resources"]["client_exit_code"] = 18446744073709551615
        cases.append(json.dumps(invalid))
        for body in cases:
            self.assertEqual(request(self.port, "experiments", method="POST", body=body)[0], 400)
        self.assertEqual(request(self.port, "experiments", method="POST", body="x" * 65537)[0], 413)
        self.assertEqual(request(self.port, "experiments", method="POST", body="x" * 131073, raw=True)[0], 413)
        self.assertEqual(request(self.port, "experiments/compare", method="POST", body='{"baseline":"../../etc/passwd"}')[0], 400)
        self.assertEqual(request(self.port, "experiments?path=elsewhere")[0], 400)


if __name__ == "__main__":
    unittest.main()
