"""Snapshot benchmark failure cleanup and evidence metadata; no load or servers."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

import benchmark_worker_snapshot as benchmark


class SnapshotBenchmarkTests(unittest.TestCase):
    def test_early_master_exit_stops_group_before_removing_config(self):
        self.check_startup_failure()

    def test_failed_cleanup_preserves_config(self):
        self.check_startup_failure(PermissionError("denied"))

    def check_startup_failure(self, cleanup_error=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "1-on/nginx.conf"
            process = Mock(pid=123, poll=Mock(return_value=1))
            reservation = MagicMock()
            reservation.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 12345)
            def stop(actual):
                self.assertIs(actual, process)
                self.assertTrue(config.exists())
                if cleanup_error:
                    raise cleanup_error
            with patch.object(benchmark.socket, "socket", return_value=reservation), \
                 patch.object(benchmark, "start_process_group", return_value=process), \
                 patch.object(benchmark, "stop_process_group", side_effect=stop) as cleanup, \
                 patch.object(benchmark, "wait_for", side_effect=RuntimeError("master exited before ready")):
                with self.assertRaises(type(cleanup_error) if cleanup_error else RuntimeError):
                    benchmark.run(Path("/unused/nginx"), "", root, 1, True, "0", "1")
                cleanup.assert_called_once_with(process)
                self.assertEqual(config.exists(), cleanup_error is not None)

    def test_default_and_explicit_build_modes_reach_load_command(self):
        for mode in (None, "release", "debug"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                reservation = MagicMock()
                reservation.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 12345)
                def sample(command, **_):
                    load = command[command.index("--") + 1:]
                    self.assertEqual(load[load.index("--build-mode") + 1], mode or "unknown")
                    raise RuntimeError("stop before load")
                with patch.object(benchmark.socket, "socket", return_value=reservation), \
                     patch.object(benchmark, "start_process_group", return_value=Mock(pid=123)), \
                     patch.object(benchmark, "stop_process_group"), patch.object(benchmark, "wait_for"), \
                     patch.object(benchmark, "children", return_value=[456, 457]), \
                     patch.object(benchmark, "run_process_group", side_effect=sample) as run:
                    with self.assertRaisesRegex(RuntimeError, "stop before load"):
                        args = () if mode is None else (mode,)
                        benchmark.run(Path("/unused/nginx"), "", Path(directory), 1, True, "0", "1", *args)
                    run.assert_called_once()
