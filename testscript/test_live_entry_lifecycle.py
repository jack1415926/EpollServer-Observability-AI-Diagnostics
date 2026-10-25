"""Actual live-test entrypoints plus real orphan children, without TCP or databases."""
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import test_backend_failure_live as backend
import test_shutdown as shutdown
import test_worker_snapshot_live as snapshot
from process_group import _members, start_process_group, stop_process_group


class LiveEntryLifecycleTests(unittest.TestCase):
    def test_early_master_exit_cleans_children_before_config(self):
        self.check_entries(False)

    def test_cleanup_failure_preserves_config(self):
        self.check_entries(True)

    def check_entries(self, reject_cleanup):
        for module in (shutdown, snapshot, backend):
            with self.subTest(entry=module.__name__, rejected=reject_cleanup), \
                 tempfile.TemporaryDirectory(prefix="epoll-entry-lifecycle-") as directory:
                root = Path(directory)
                binary = root / "nginx"
                binary.touch()
                binary.with_suffix(".conf").write_text("")
                config = root / ("nginx.conf" if module is backend else "case/nginx.conf")
                processes = []
                def start(*_, **__):
                    script = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])"
                    process = start_process_group([sys.executable, "-c", script])
                    processes.append(process)
                    process.wait(timeout=5)
                    self.assertTrue(_members(process))
                    return process
                def stop(process):
                    self.assertTrue(config.exists())
                    self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                    if reject_cleanup:
                        raise PermissionError("injected cleanup rejection")
                    stop_process_group(process, timeout=0.2)
                reservation = MagicMock()
                reservation.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 12345)
                try:
                    with ExitStack() as patches:
                        patches.enter_context(patch.object(module.socket, "socket", return_value=reservation))
                        patches.enter_context(patch.object(module, "start_process_group", side_effect=start))
                        cleanup = patches.enter_context(patch.object(module, "stop_process_group", side_effect=stop))
                        if module is backend:
                            patches.enter_context(patch.object(module.tempfile, "mkdtemp", return_value=str(root)))
                            patches.enter_context(patch.object(module.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0)))
                            patches.enter_context(patch.object(sys, "argv", ["backend", "--binary", str(binary)]))
                        with self.assertRaises(PermissionError if reject_cleanup else ValueError):
                            if module is shutdown:
                                shutdown.run_case(binary, "", root, "case", 15)
                            elif module is snapshot:
                                snapshot.run_case(binary, "", root, "case")
                            else:
                                backend.main()
                        cleanup.assert_called_once_with(processes[0])
                        self.assertEqual(config.exists(), reject_cleanup)
                        if not reject_cleanup:
                            self.assertTrue(processes[0].group_stopped)
                            self.assertFalse(_members(processes[0]))
                finally:
                    # Own and verify every real group before TemporaryDirectory can remove files.
                    for process in processes:
                        stop_process_group(process, timeout=0.2)
