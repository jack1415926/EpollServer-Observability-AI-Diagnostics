"""Compile the real C++ reader; isolate filesystem and /proc fixtures without a server."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
try:
    from .process_group import start_process_group, stop_process_group, remove_process_directory
except ImportError:
    from process_group import start_process_group, stop_process_group, remove_process_directory

ROOT = Path(__file__).resolve().parents[1]
BOOT = "11111111-2222-3333-4444-555555555555"


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux BOOTTIME and /proc")
class MetricsCollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="epoll-collector-build-")
        cls.addClassCleanup(cls.build.cleanup)
        cls.binary = Path(cls.build.name) / "probe"
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        "-I" + str(ROOT / "monitor/collector"),
                        "-I" + os.environ.get("JSON_INCLUDE", str(ROOT / ".deps/usr/include")),
                        str(ROOT / "monitor/collector/collector.cxx"),
                        str(ROOT / "monitor/collector/event_rules.cxx"),
                        str(ROOT / "testscript/collector_probe.cxx"), "-o", str(cls.binary)], check=True, timeout=90)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="epoll-collector-test-"))
        self.processes = []
        self.addCleanup(remove_process_directory, self.root, self.processes)
        self.metrics = self.root / "metrics"
        self.metrics.mkdir(mode=0o700)
        self.proc = self.root / "proc"
        boot = self.proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text(BOOT + "\n")
        self.process(100, 10, 1)
        self.process(101, 11, 100)
        self.process(102, 12, 100)
        self.master = {"schema_version": 1, "boot_id": BOOT, "master_pid": 100, "master_start_ticks": 10,
                       "sequence": 1, "write_failures_total": 0, "state": "running", "expected_workers": 2,
                       "workers": [{"worker_index": 0, "pid": 101, "process_start_ticks": 11},
                                   {"worker_index": 1, "pid": 102, "process_start_ticks": 12}]}
        self.manifest = self.metrics / f"master-{BOOT}-100-10.json"
        self.write(self.manifest, self.master)
        self.worker_files = []
        self.samples = []
        for index, pid, ticks in [(0, 101, 11), (1, 102, 12)]:
            snapshot = {"schema_version": 1, "boot_id": BOOT, "master_pid": 100, "master_start_ticks": 10,
                        "worker_index": index, "pid": pid, "process_start_ticks": ticks,
                        "sampled_at_unix_ms": int(time.time() * 1000),
                        "sampled_at_boottime_ms": self.now() - 50,
                        "sequence": 1, "collection_ok": True, "write_failures_total": 0,
                        "connections_current": index + 2, "receive_queue_messages": index + 4,
                        "send_queue_messages": index + 6}
            file = self.metrics / f"worker-{BOOT}-{pid}-{ticks}.json"
            self.write(file, snapshot)
            self.worker_files.append(file)
            self.samples.append(snapshot)

    @staticmethod
    def now():
        return int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000)

    def write(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def process(self, pid, ticks, parent, state="S"):
        file = self.proc / str(pid) / "stat"
        file.parent.mkdir(exist_ok=True, parents=True)
        # A comm with spaces and ')' catches naive /proc stat tokenization.
        file.write_text(f"{pid} (name with ) spaces) {state} {parent} " + "0 " * 17 + str(ticks) + "\n")

    def collect(self, age=3000, manifest=None):
        result = subprocess.run([str(self.binary), str(manifest or self.manifest), str(age), str(self.proc)],
                                capture_output=True, text=True, timeout=5, check=True)
        return json.loads(result.stdout)

    def resource_process(self, cpu, rss=40, ticks=11, parent=100):
        fields = ['S', str(parent)] + ['0'] * 20
        fields[11], fields[12], fields[19], fields[21] = str(cpu), '0', str(ticks), str(rss)
        (self.proc / '101/stat').write_text('101 (name with ) spaces) ' + ' '.join(fields) + '\n')

    def resource_probe(self):
        probe = start_process_group([str(self.binary), str(self.manifest), '3000', str(self.proc), 'resources'],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.processes.append(probe)
        self.addCleanup(stop_process_group, probe, timeout=1)
        self.addCleanup(probe.stdin.close)
        self.addCleanup(probe.stdout.close)
        def take(whole=False):
            import select
            probe.stdin.write('sample\n'); probe.stdin.flush()
            self.assertTrue(select.select([probe.stdout], [], [], 5)[0], 'resource probe timed out')
            value = json.loads(probe.stdout.readline())
            return value if whole else value['workers'][0]
        return take

    def test_resource_rates_rss_and_stale_business_are_independent(self):
        self.resource_process(100)
        take = self.resource_probe()
        first = take()['resources']
        self.assertIsNone(first['cpu_percent'])
        self.assertEqual(first['reason'], 'cpu_warming_up')
        self.assertEqual(first['rss_bytes'], 40 * os.sysconf('SC_PAGE_SIZE'))
        self.write(self.worker_files[0], {**self.samples[0], 'sampled_at_boottime_ms': self.now()-10000})
        time.sleep(.05)
        self.resource_process(120)
        row = take(); value = row['resources']
        self.assertEqual(row['status'], 'stale')
        self.assertIsNone(row['metrics']['connections_current'])
        self.assertEqual(value['status'], 'available')
        self.assertAlmostEqual(value['cpu_percent'], 20*100000/os.sysconf('SC_CLK_TCK')/value['interval_ms'])
        self.assertGreater(value['cpu_percent'], 100, 'multi-core percentages are not clamped')

    def test_resource_reset_failures_and_identity_reuse(self):
        self.resource_process(100)
        take = self.resource_probe(); take()
        time.sleep(.02); self.resource_process(99)
        self.assertEqual(take()['resources']['reason'], 'cpu_counter_reset')
        self.resource_process(110, rss='bad')
        time.sleep(.02)
        value = take()['resources']
        self.assertIsNotNone(value['cpu_percent']); self.assertIsNone(value['rss_bytes'])
        self.assertEqual(value['reason'], 'rss_unavailable')
        self.resource_process('bad', rss=50)
        value = take()['resources']
        self.assertIsNone(value['cpu_percent']); self.assertEqual(value['rss_bytes'], 50*os.sysconf('SC_PAGE_SIZE'))
        self.resource_process(120)
        self.assertEqual(take()['resources']['reason'], 'cpu_warming_up')
        self.resource_process(130, ticks=999)
        row = take()
        self.assertEqual(row['resources']['reason'], 'identity_mismatch')
        self.assertIsNone(row['resources']['rss_bytes'])
        self.resource_process(140)
        self.assertEqual(take()['resources']['reason'], 'cpu_warming_up')
        (self.proc/'101/stat').unlink()
        self.assertEqual(take()['resources']['reason'], 'dead')
        self.resource_process(150)
        self.assertEqual(take()['resources']['reason'], 'cpu_warming_up')

    def test_resource_baseline_does_not_cross_master_restart_or_failed_manifest(self):
        self.resource_process(100)
        take = self.resource_probe(); take()
        self.process(100, 99, 1)
        self.write(self.manifest, {**self.master, 'master_start_ticks':99})
        # Invalid instance clears all baselines before a formerly valid identity returns.
        take(whole=True)
        self.process(100, 10, 1); self.write(self.manifest, self.master)
        self.assertEqual(take()['resources']['reason'], 'cpu_warming_up')
        self.write(self.manifest, {**self.master, 'workers':[]})
        self.assertEqual(take(whole=True)['workers'], [])
        self.write(self.manifest, self.master)
        self.assertEqual(take()['resources']['reason'], 'cpu_warming_up')

    def assert_partial(self, result, status):
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["valid_workers"], 1)
        self.assertEqual(result["workers"][0]["status"], status)
        self.assertEqual(result["partial_sums"]["connections_current"], 3)
        self.assertIsNone(result["totals"]["connections_current"])
        self.assertIsNone(result["workers"][0]["metrics"]["connections_current"])

    def test_complete_and_only_declared_workers_are_summed(self):
        # Orphan/stale files are neither discovered nor admitted by directory scanning.
        (self.metrics / "worker-orphan.json").write_text("invalid")
        (self.metrics / "master-orphan.json").write_text("invalid")
        result = self.collect()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["valid_workers"], 2)
        self.assertEqual(result["totals"], {"connections_current": 5, "receive_queue_messages": 9, "send_queue_messages": 13})
        self.assertEqual(result["partial_sums"], result["totals"])
        self.assertEqual(result["tcp_health"], "not_probed")

    def test_missing_corrupt_unsupported_and_typed_worker_fields(self):
        file = self.worker_files[0]
        for value, status in [(None, "missing"), ("{", "invalid_json"),
                              ({**self.samples[0], "schema_version": 2}, "unsupported_schema"),
                              ({**self.samples[0], "connections_current": -1}, "invalid_fields"),
                              ({**self.samples[0], "connections_current": True}, "invalid_fields"),
                              ({**self.samples[0], "connections_current": 1.0}, "invalid_fields"),
                              ({**self.samples[0], "collection_ok": "true"}, "invalid_fields")]:
            with self.subTest(value=value):
                if value is None:
                    file.unlink()
                elif isinstance(value, str):
                    file.write_text(value)
                    file.chmod(0o600)
                else:
                    self.write(file, value)
                self.assert_partial(self.collect(), status)

    def test_worker_identity_mismatch_and_parent_pid_reuse(self):
        for key, value in [("boot_id", "old"), ("master_pid", 99), ("master_start_ticks", 9),
                           ("pid", 999), ("process_start_ticks", 999), ("worker_index", 1)]:
            with self.subTest(key=key):
                self.write(self.worker_files[0], {**self.samples[0], key: value})
                self.assert_partial(self.collect(), "snapshot_identity_mismatch")
        self.write(self.worker_files[0], self.samples[0])
        self.process(101, 999, 100)
        self.assert_partial(self.collect(), "identity_mismatch")
        self.process(101, 11, 999)
        self.assert_partial(self.collect(), "identity_mismatch")

    def test_event_rules_distinguish_snapshot_and_process_identity(self):
        def events():
            output = subprocess.check_output([str(self.binary), str(self.manifest), "3000",
                                              str(self.proc), "events"], text=True, timeout=5)
            return {event["rule"] for event in json.loads(output)}
        self.write(self.worker_files[0], {**self.samples[0], "master_start_ticks": 999})
        self.assertEqual(events(), {"collection_unavailable"})
        self.write(self.worker_files[0], self.samples[0])
        self.process(101, 999, 100)
        self.assertEqual(events(), {"collection_unavailable", "worker_process_anomaly"})

    def test_expired_future_and_collection_failed(self):
        for changes, status in [({"sampled_at_boottime_ms": self.now() - 10000}, "stale"),
                                ({"sampled_at_boottime_ms": self.now() + 10000}, "future_sample"),
                                ({"collection_ok": False}, "collection_failed")]:
            with self.subTest(status=status):
                self.write(self.worker_files[0], {**self.samples[0], **changes})
                self.assert_partial(self.collect(), status)
        # Wall clock age is deliberately irrelevant to same-boot freshness.
        self.write(self.worker_files[0], {**self.samples[0], "sampled_at_unix_ms": 0})
        self.assertEqual(self.collect()["status"], "complete")

    def test_dead_zombie_and_unreadable_process(self):
        self.process(101, 11, 100, "Z")
        self.assert_partial(self.collect(), "dead")
        (self.proc / "101/stat").unlink()
        self.assert_partial(self.collect(), "dead")
        (self.proc / "101/stat").write_text("broken")
        self.assert_partial(self.collect(), "process_unavailable")

    def test_no_valid_workers_never_returns_fabricated_zero(self):
        for file in self.worker_files:
            file.unlink()
        result = self.collect()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["valid_workers"], 0)
        self.assertIsNone(result["partial_sums"]["connections_current"])
        self.assertIsNone(result["totals"]["connections_current"])

    def test_master_missing_malformed_and_invalid_identities(self):
        for changes, reason in [({"schema_version": 99}, "unsupported_schema"),
                                ({"boot_id": "old"}, "master_identity_mismatch"),
                                ({"expected_workers": False}, "invalid_fields"),
                                ({"workers": [self.master["workers"][0]] * 2}, "duplicate_worker_identity")]:
            with self.subTest(changes=changes):
                self.write(self.manifest, {**self.master, **changes})
                result = self.collect()
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["reason"], reason)
        self.manifest.unlink()
        self.assertEqual(self.collect()["reason"], "missing")

    def test_master_pid_reuse_exit_and_name_mismatch(self):
        self.process(100, 999, 1)
        self.assertEqual(self.collect()["reason"], "identity_mismatch")
        self.process(100, 10, 1, "Z")
        self.assertEqual(self.collect()["reason"], "dead")
        self.process(100, 10, 1)
        wrong_name = self.manifest.with_name("master-wrong.json")
        self.write(wrong_name, self.master)
        self.assertEqual(self.collect(manifest=wrong_name)["reason"], "master_identity_mismatch")

    def test_expected_count_is_not_roster_length_and_stopping_is_not_complete(self):
        self.write(self.manifest, {**self.master, "workers": self.master["workers"][:1]})
        self.assertEqual(self.collect()["status"], "partial")
        self.write(self.manifest, {**self.master, "state": "stopping"})
        result = self.collect()
        self.assertEqual(result["status"], "stopping")
        self.assertIsNone(result["totals"]["connections_current"])

    def test_unsafe_directory_symlink_fifo_oversize_and_duplicate_json_keys(self):
        file = self.worker_files[0]
        original = file.read_text()
        file.unlink()
        file.symlink_to(self.worker_files[1])
        self.assert_partial(self.collect(), "unreadable")
        file.unlink()
        os.mkfifo(file, 0o600)
        self.assert_partial(self.collect(), "invalid_file")
        file.unlink()
        file.write_text("x" * 16385)
        file.chmod(0o600)
        self.assert_partial(self.collect(), "invalid_file")
        file.write_text(original[:-1] + ', "pid": 101}')
        self.assert_partial(self.collect(), "invalid_json")
        self.metrics.chmod(0o777)
        self.assertEqual(self.collect()["reason"], "directory_unavailable")
        self.metrics.chmod(0o700)

    def collect_during_change(self, pid, change, age=3000):
        # A synthetic /proc FIFO pauses the actual reader at the first Worker check.
        # Restore a regular stat file before resuming so later identity reads proceed.
        file = self.proc / str(pid) / "stat"
        text = file.read_bytes()
        file.unlink()
        os.mkfifo(file, 0o600)
        errors = []
        def writer():
            try:
                with file.open("wb", buffering=0) as stream:
                    file.unlink()
                    self.process(pid, pid - 90, 100)
                    change()
                    stream.write(text)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        result = self.collect(age=age)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        return result

    def test_directory_symlink_suffixes_are_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.metrics, target_is_directory=True)
        for suffix in ("", "/", "/.", "/./"):
            manifest = str(link) + suffix + "/" + self.manifest.name
            with self.subTest(suffix=suffix):
                self.assertEqual(self.collect(manifest=manifest)["reason"], "directory_unavailable")

    def test_expiry_during_final_identity_check_discards_sums(self):
        # Pause the second Worker, replace the first stat with a FIFO, then delay
        # only its final identity check. No production /proc file is modified.
        first = self.proc / "101/stat"
        text = first.read_bytes()
        errors = []
        threads = []
        def final_check():
            try:
                with first.open("wb", buffering=0) as stream:
                    time.sleep(0.25)
                    first.unlink()
                    first.write_bytes(text)
                    stream.write(text)
            except Exception as error:
                errors.append(error)
        def install_fifo():
            # Refresh samples after the compiler/test runner's scheduling delays.
            for file, sample in zip(self.worker_files, self.samples):
                self.write(file, {**sample, "sampled_at_boottime_ms": self.now()})
            first.unlink()
            os.mkfifo(first, 0o600)
            thread = threading.Thread(target=final_check, daemon=True)
            thread.start()
            threads.append(thread)
        result = self.collect_during_change(102, install_fifo, age=200)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result["status"], "instance_changed")
        self.assertEqual(result["valid_workers"], 0)
        self.assertIsNone(result["totals"]["connections_current"])

    def test_membership_change_during_read_discards_all_sums(self):
        result = self.collect_during_change(101, lambda: self.write(
            self.manifest, {**self.master, "sequence": 2, "state": "stopping"}))
        self.assertEqual(result["status"], "instance_changed")
        self.assertEqual(result["workers"], [])
        self.assertEqual(result["valid_workers"], 0)
        self.assertIsNone(result["partial_sums"]["connections_current"])

    def test_previously_read_worker_exits_during_collection(self):
        result = self.collect_during_change(102, lambda: self.process(101, 11, 100, "Z"))
        self.assertEqual(result["status"], "instance_changed")
        self.assertEqual(result["workers"], [])
        self.assertIsNone(result["totals"]["connections_current"])


if __name__ == "__main__":
    unittest.main()
