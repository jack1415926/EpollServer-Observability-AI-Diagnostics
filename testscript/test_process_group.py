"""Owned, short-lived processes exercise timeout and early-parent-exit cleanup."""
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from process_group import _members, run_process_group, start_process_group, stop_process_group, remove_process_directory


class ProcessGroupTests(unittest.TestCase):
    def test_directory_survives_unconfirmed_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "owned"
            directory.mkdir()
            marker = directory / "keep"
            marker.touch()
            processes = [Mock(group_stopped=True), Mock(group_stopped=False)]
            with self.assertRaisesRegex(RuntimeError, "directory preserved"):
                remove_process_directory(directory, processes)
            self.assertTrue(marker.exists())
            processes[1].group_stopped = True
            remove_process_directory(directory, processes)
            self.assertFalse(directory.exists())

    def test_timeout_stops_client_and_grandchild(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "survived"
            child = f"import time; time.sleep(1); open({str(marker)!r}, 'w').close()"
            parent = ("import subprocess,sys,time; "
                      f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(60)")
            with self.assertRaises(subprocess.TimeoutExpired):
                run_process_group([sys.executable, "-c", parent], timeout=0.4)
            time.sleep(1)
            self.assertFalse(marker.exists())

    def test_exited_master_does_not_hide_worker(self):
        parent = ("import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
                  "'import time; time.sleep(60)'])")
        process = start_process_group([sys.executable, "-c", parent])
        try:
            process.wait(timeout=5)
            self.assertTrue(_members(process))
            stop_process_group(process, timeout=0.2)
            self.assertFalse(_members(process))
        finally:
            stop_process_group(process, timeout=0.2)

    def test_kill_fallback_and_other_session_protected(self):
        ready = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
        process = start_process_group([sys.executable, "-c", ready], stdout=subprocess.PIPE, text=True)
        unrelated = subprocess.Popen(["sleep", "60"])
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            stop_process_group(process, timeout=0.1)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertIsNone(unrelated.poll())
        finally:
            stop_process_group(process, timeout=0.1)
            process.stdout.close()
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_identity_change_prevents_signalling(self):
        process = start_process_group(["sleep", "60"])
        ticks = process.group_start_ticks
        try:
            process.group_start_ticks += 1
            with patch("process_group.os.killpg") as send:
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    stop_process_group(process, timeout=0.1)
                send.assert_not_called()
        finally:
            process.group_start_ticks = ticks
            stop_process_group(process, timeout=0.1)


if __name__ == "__main__":
    unittest.main()
