"""Linux-only publisher tests; no running server, MySQL or Redis required."""
import json
import os
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

    def start(self, path):
        proc = subprocess.Popen([str(self.binary), str(path)], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                text=True)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            proc.stdin.close()
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
            with self.subTest(path=path.name):
                self.assertEqual(self.start(path).wait(timeout=5), 2)
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


if __name__ == "__main__":
    unittest.main()
