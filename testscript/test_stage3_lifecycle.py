"""Fault paths use mocked processes only; never start or signal a database."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_stage3_live as live


class Stage3LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="epoll-lifecycle-test-")
        self.addCleanup(self.directory.cleanup)
        self.evidence = Path(self.directory.name) / "evidence"
        self.database = live.TestDatabase(self.evidence)
        # All process objects in these fixtures are mocks, so no active directory exists.
        self.addCleanup(lambda: shutil.rmtree(self.database.root, ignore_errors=True))
        (self.database.root / "data").mkdir()
        self.marker = self.database.root / "data" / "keep"
        self.marker.write_text("owned fixture")

    def process(self, running=True):
        process = Mock()
        process.pid = 12345
        process.poll.return_value = None if running else 0
        process.returncode = None if running else 0
        return process

    def test_initializer_timeout_tracks_process_and_preserves_data(self):
        initializer = self.process()
        timeout = subprocess.TimeoutExpired(["mysqld", "--initialize-insecure"], 90)
        initializer.wait.side_effect = timeout
        with patch.object(live.subprocess, "Popen", return_value=initializer), \
             patch.object(live.subprocess, "run", side_effect=timeout):
            with self.assertRaises((subprocess.TimeoutExpired, RuntimeError)):
                self.database.__enter__()
        self.assertIs(self.database.process, initializer)
        self.assertTrue(self.marker.exists())
        initializer.kill.assert_not_called()

    def test_initializer_failure_archives_then_removes_inactive_directory(self):
        initializer = self.process(False)
        initializer.returncode = 1
        initializer.poll.return_value = 1
        def fail(*_, **kwargs):
            kwargs["stdout"].write(b"initialization failed")
            return initializer
        with patch.object(live.subprocess, "Popen", side_effect=fail), \
             patch.object(live.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"initialization failed")):
            with self.assertRaises(ValueError):
                self.database.__enter__()
        self.assertFalse(self.database.root.exists())
        self.assertIn("initialization failed", (self.evidence / "init-console.log").read_text())

    def test_startup_spawn_failure_cleans_completed_initializer(self):
        initializer = self.process(False)
        initializer.communicate.return_value = (b"", b"")
        with patch.object(live.subprocess, "Popen", side_effect=[initializer, OSError("spawn failed")]), \
             patch.object(live.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")), \
             patch.object(live, "free_port", return_value=32123):
            with self.assertRaises(OSError):
                self.database.__enter__()
        self.assertFalse(self.database.root.exists())

    def test_shutdown_rejection_preserves_live_database(self):
        self.database.process = self.process()
        self.database.admin = ["mysql", "private-socket"]
        self.database.initializing = False
        with patch.object(live.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["mysql"])):
            with self.assertRaises(subprocess.CalledProcessError):
                self.database.__exit__(None, None, None)
        self.assertTrue(self.marker.exists())
        self.database.process.wait.assert_not_called()

    def test_shutdown_wait_timeout_preserves_live_database(self):
        self.database.process = self.process()
        self.database.admin = ["mysql", "private-socket"]
        self.database.initializing = False
        self.database.process.wait.side_effect = subprocess.TimeoutExpired(["mysqld"], 30)
        with patch.object(live.subprocess, "run"):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.database.__exit__(None, None, None)
        self.assertTrue(self.marker.exists())

    def test_successful_shutdown_waits_before_removing_directory(self):
        self.evidence.mkdir()
        self.database.process = self.process()
        self.database.admin = ["mysql", "private-socket"]
        self.database.initializing = False
        self.database.process.wait.side_effect = lambda **_: self.assertTrue(self.marker.exists())
        with patch.object(live.subprocess, "run") as shutdown:
            self.database.__exit__(None, None, None)
        shutdown.assert_called_once()
        self.database.process.wait.assert_called_once_with(timeout=30)
        self.assertFalse(self.database.root.exists())

    def test_http_cleanup_failure_still_attempts_tcp_and_database_cleanup(self):
        database, monitor, tcp = Mock(), self.process(), self.process()
        database.__exit__ = Mock()
        monitor.terminate.side_effect = PermissionError("signal denied")
        config = Path(self.directory.name) / "nginx.conf"
        config.write_text("fixture")
        with patch.object(live, "stop_process_group") as stop:
            with self.assertRaises(PermissionError):
                live.cleanup_stage3(database, monitor, tcp, config)
        stop.assert_called_once_with(tcp)
        database.__exit__.assert_called_once_with(None, None, None)
        self.assertFalse(config.exists())

    def test_tcp_cleanup_failure_preserves_config_and_still_stops_database(self):
        database, tcp = Mock(), self.process()
        database.__exit__ = Mock()
        config = Path(self.directory.name) / "nginx.conf"
        config.write_text("fixture")
        with patch.object(live, "stop_process_group", side_effect=PermissionError("signal denied")):
            with self.assertRaises(PermissionError):
                live.cleanup_stage3(database, None, tcp, config)
        database.__exit__.assert_called_once_with(None, None, None)
        self.assertTrue(config.exists())
