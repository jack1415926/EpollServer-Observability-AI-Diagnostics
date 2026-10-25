"""Real sampler/client groups with injected collection failures; no performance load."""
import json
import os
from pathlib import Path
import signal
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import performance_sampler as sampler
from process_group import _members, start_process_group, stop_process_group


class SamplerLifecycleTests(unittest.TestCase):
    def test_cli_term_cleans_separate_client_session(self):
        root = Path(tempfile.mkdtemp(prefix="epoll-sampler-cli-"))
        ready = root / "client.json"
        script = ("import json,os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                  "ticks=int(open('/proc/self/stat').read().rsplit(')',1)[1].split()[19]); "
                  f"open({str(ready)!r},'w').write(json.dumps([os.getpid(),ticks])); time.sleep(60)")
        process = None
        client = None
        try:
            with (root / "console.log").open("wb") as output:
                process = start_process_group([sys.executable, str(Path(sampler.__file__)),
                    "--master-pid", str(os.getpid()), "--label", "lifecycle", "--interval", "0.05",
                    "--output", str(root / "result.json"), "--", sys.executable, "-c", script],
                    stdout=output, stderr=output)
                deadline = time.monotonic() + 5
                while not ready.exists() or not ready.stat().st_size:
                    self.assertIsNone(process.poll())
                    if time.monotonic() > deadline:
                        self.fail("sampler client did not start")
                    time.sleep(0.01)
                pid, ticks = json.loads(ready.read_text())
                client = SimpleNamespace(pid=pid, group_start_ticks=ticks)
                self.assertTrue(_members(client))
                stop_process_group(process, timeout=5) # Actual OS SIGTERM to the sampler.
                self.assertEqual(process.returncode, 143)
                self.assertFalse(_members(client))
        finally:
            if process is not None:
                stop_process_group(process, timeout=5)
            # If the assertion found a regression, still clean only this verified client group.
            if client is not None and _members(client):
                os.killpg(client.pid, signal.SIGKILL)
                deadline = time.monotonic() + 5
                while _members(client):
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"client still alive; preserve {root}")
                    time.sleep(0.01)
            shutil.rmtree(root)

    def test_success_reaps_client_and_leftover_child(self):
        self.check_run("success")

    def test_collection_failure_stops_client_and_child(self):
        self.check_run("failure")

    def test_term_handler_stops_client_and_child_and_is_restored(self):
        self.check_run("term")

    def check_run(self, mode):
        with tempfile.TemporaryDirectory(prefix="epoll-sampler-lifecycle-") as directory:
            root = Path(directory)
            ready = root / "ready"
            child = "import time; time.sleep(60)"
            script = ("import subprocess,sys,time; "
                      f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
                      f"open({str(ready)!r},'w').close(); " + ("" if mode == "success" else "time.sleep(60)"))
            args = SimpleNamespace(master_pid=os.getpid(), label="lifecycle", log=root / "absent.log",
                                   interval=0.05, output=root / "result.json", command=[sys.executable, "-c", script])
            processes = []
            handler = signal.getsignal(signal.SIGTERM)
            def start(command):
                process = start_process_group(command)
                processes.append(process)
                return process
            def discover():
                deadline = time.monotonic() + 5
                while not ready.exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("client did not start")
                    time.sleep(0.01)
                if mode == "failure":
                    raise OSError("injected sampling failure")
                if mode == "term":
                    installed = signal.getsignal(signal.SIGTERM)
                    self.assertIsNot(installed, handler)
                    installed(signal.SIGTERM, None)
                return []
            try:
                with patch.object(sampler, "parse_args", return_value=args), \
                     patch.object(sampler, "start_process_group", side_effect=start), \
                     patch.object(sampler, "find_redis_pids", side_effect=discover):
                    if mode == "success":
                        self.assertEqual(sampler.main(), 0)
                        self.assertEqual(json.loads(args.output.read_text())["client_exit_code"], 0)
                    elif mode == "failure":
                        with self.assertRaisesRegex(OSError, "injected sampling failure"):
                            sampler.main()
                    else:
                        with self.assertRaises(SystemExit) as raised:
                            sampler.main()
                        self.assertEqual(raised.exception.code, 143)
                self.assertEqual(len(processes), 1)
                self.assertTrue(processes[0].group_stopped)
                self.assertFalse(_members(processes[0]))
                self.assertIs(signal.getsignal(signal.SIGTERM), handler)
            finally:
                for process in processes:
                    stop_process_group(process, timeout=0.2)
