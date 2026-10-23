"""Linux-only publisher tests; no running server, MySQL or Redis required."""
import json
import os
import select
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def wait_for(check, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("snapshot condition not reached before timeout")


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux /proc and POSIX files")
class WorkerSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="epoll-snapshot-build-")
        cls.addClassCleanup(cls.build.cleanup)
        cls.binary = Path(cls.build.name) / "probe"
        subprocess.run([
            "g++", "-std=c++17", "-pthread", "-Wall", "-Wextra", "-Werror",
            "-I" + str(ROOT / "server/include"),
            str(ROOT / "testscript/worker_snapshot_probe.cxx"),
            str(ROOT / "server/misc/ngx_worker_snapshot.cxx"),
            "-o", str(cls.binary),
        ], check=True, timeout=60)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="epoll-snapshot-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def start(self, path, master=False):
        proc = subprocess.Popen([str(self.binary), str(path)] + (["--master"] if master else []), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE if master else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                text=True)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            proc.stdin.close()
            if proc.stdout is not None:
                proc.stdout.close()
        self.addCleanup(cleanup)
        return proc

    def snapshot(self, directory):
        files = list(directory.glob("worker-*.json"))
        return json.loads(files[0].read_text()) if files else None

    def test_live_values_identity_atomic_readers_and_cleanup(self):
        path = self.root / "metrics"
        proc = self.start(path)
        first = wait_for(lambda: self.snapshot(path))
        self.assertEqual(first["pid"], proc.pid)
        self.assertEqual(first["master_pid"], os.getpid())
        self.assertEqual(first["worker_index"], 7)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["boot_id"], Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        self.assertGreater(first["process_start_ticks"], 0)
        self.assertGreater(first["master_start_ticks"], 0)
        self.assertGreater(first["sampled_at_boottime_ms"], 0)
        self.assertLess(abs(first["sampled_at_unix_ms"] - time.time() * 1000), 5000)
        self.assertTrue(first["collection_ok"])
        self.assertEqual(first["write_failures_total"], 0)
        self.assertEqual(first["connections_current"], 3)
        self.assertEqual(first["receive_queue_messages"], 5)
        self.assertEqual(first["send_queue_messages"], 7)
        file = next(path.glob("*.json"))
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        proc.stdin.write("11 13 17\n")
        proc.stdin.flush()
        # Repeatedly parse while the publisher replaces the file.
        def updated():
            current = self.snapshot(path)
            self.assertGreaterEqual(current["sequence"], first["sequence"])
            values = (current["connections_current"], current["receive_queue_messages"],
                      current["send_queue_messages"])
            return current if values == (11, 13, 17) else None
        current = wait_for(updated)
        self.assertEqual((current["receive_queue_messages"], current["send_queue_messages"]), (13, 17))
        self.assertGreater(current["sequence"], first["sequence"])
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        self.assertEqual(proc.wait(timeout=5), 0)
        self.assertEqual(list(path.iterdir()), [])

    def test_reject_file_symlink_and_writable_directory(self):
        target = self.root / "file"
        target.write_text("keep")
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        for path in (target, link, unsafe):
            for master in (False, True):
                with self.subTest(path=path.name, master=master):
                    self.assertEqual(self.start(path, master).wait(timeout=5), 2)
        for suffix in ("/", "/.", "/./"):
            for master in (False, True):
                with self.subTest(suffix=suffix, master=master):
                    self.assertEqual(self.start(str(link) + suffix, master).wait(timeout=5), 2)
        self.assertEqual(target.read_text(), "keep")

    def test_write_failure_and_recovery(self):
        path = self.root / "metrics"
        proc = self.start(path)
        wait_for(lambda: self.snapshot(path))
        file = next(path.glob("*.json"))
        file.unlink()
        file.mkdir()  # rename cannot replace a directory, even when running as root.
        time.sleep(1.3)
        self.assertIsNone(proc.poll())
        self.assertEqual(list(path.glob("*.tmp")), [])
        file.rmdir()
        current = wait_for(lambda: self.snapshot(path))
        self.assertGreaterEqual(current["write_failures_total"], 1)

    def test_crash_retains_snapshot_and_restart_has_distinct_identity(self):
        path = self.root / "metrics"
        first_proc = self.start(path)
        first = wait_for(lambda: self.snapshot(path))
        first_proc.kill()
        first_proc.wait(timeout=5)
        second_proc = self.start(path)
        files = wait_for(lambda: list(path.glob("*.json")) if len(list(path.glob("*.json"))) == 2 else None)
        snapshots = [json.loads(file.read_text()) for file in files]
        self.assertEqual({value["pid"] for value in snapshots}, {first["pid"], second_proc.pid})

    def test_master_lifecycle_failure_recovery_and_cleanup(self):
        path = self.root / "metrics"
        proc = self.start(path, master=True)
        def snapshot():
            files = list(path.glob("master-*.json"))
            return json.loads(files[0].read_text()) if files else None
        first = wait_for(snapshot)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["master_pid"], proc.pid)
        self.assertEqual(first["state"], "running")
        self.assertEqual(first["expected_workers"], 2)
        self.assertEqual(first["boot_id"], Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        ticks = int(Path(f"/proc/{proc.pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        self.assertEqual(first["master_start_ticks"], ticks)
        self.assertEqual(first["workers"], [{"worker_index": 7, "pid": proc.pid, "process_start_ticks": ticks}])
        file = next(path.glob("master-*.json"))
        self.assertEqual(file.name, f"master-{first['boot_id']}-{proc.pid}-{ticks}.json")
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        for _ in range(20):
            proc.stdin.write("stopping\n")
            proc.stdin.flush()
            # Reopen and parse the atomically replaced file while the writer runs.
            def replaced():
                self.assertGreaterEqual(snapshot()["sequence"], first["sequence"])
                self.assertIsNone(proc.poll())
                return select.select([proc.stdout], [], [], 0)[0]
            wait_for(replaced)
            self.assertEqual(proc.stdout.readline().strip(), "updated")
        self.assertEqual(snapshot()["state"], "stopping")
        self.assertEqual(snapshot()["workers"], first["workers"])
        # Observe a failed lifecycle publication, then recovery on the next update.
        file.unlink()
        file.mkdir()
        proc.stdin.write("stopping\n")
        proc.stdin.flush()
        self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
        self.assertEqual(proc.stdout.readline().strip(), "updated")
        self.assertEqual(list(path.glob("*.tmp")), [])
        file.rmdir()
        proc.stdin.write("empty\n")
        proc.stdin.flush()
        final = wait_for(snapshot)
        self.assertEqual(final["state"], "stopping")
        self.assertEqual(final["workers"], [])
        self.assertEqual(final["write_failures_total"], 1)
        self.assertGreater(final["sequence"], first["sequence"])
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        self.assertEqual(proc.wait(timeout=5), 0)
        self.assertEqual(list(path.iterdir()), [])

    def test_master_crash_retains_distinct_instance_file(self):
        path = self.root / "metrics"
        first = self.start(path, master=True)
        wait_for(lambda: list(path.glob("master-*.json")))
        first.kill()
        first.wait(timeout=5)
        second = self.start(path, master=True)
        files = wait_for(lambda: list(path.glob("master-*.json")) if len(list(path.glob("master-*.json"))) == 2 else None)
        self.assertEqual({json.loads(file.read_text())["master_pid"] for file in files}, {first.pid, second.pid})


if __name__ == "__main__":
    unittest.main()
