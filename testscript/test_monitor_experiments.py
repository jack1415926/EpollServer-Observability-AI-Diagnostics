"""Exercise report generation without running a TCP server or performance load."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from experiment_fixture import valid_report
import run_monitor_experiments as runner


class MonitorExperimentTests(unittest.TestCase):
    def fake_sample(self, command, **_):
        load = command[command.index("--") + 1:]
        option = lambda key: load[load.index(key) + 1]
        fixture = valid_report()
        benchmark = fixture["benchmark"]
        benchmark["label"] = option("--label")
        benchmark["server"]["build_mode"] = option("--build-mode")
        requested = int(option("--concurrency")) * int(option("--requests-per-client"))
        benchmark["load"].update(concurrency=int(option("--concurrency")),
                                 requests_per_client=int(option("--requests-per-client")), requested=requested)
        benchmark["result"].update(attempted=requested, successful=requested, qps=requested)
        benchmark["result"]["latency_ms"]["sample_count"] = requested
        Path(option("--json-out")).write_text(json.dumps(benchmark))
        resources = {"label": benchmark["label"], "command": load,
                     "started_utc": "2026-10-23T23:59:59+00:00", **fixture["resources"]}
        Path(command[command.index("--output") + 1]).write_text(json.dumps(resources))

    def test_unique_labels_and_explicit_build_mode(self):
        labels = set()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "nginx"
            binary.write_bytes(b"mock binary")
            binary.with_suffix(".conf").write_text("")
            with patch.object(runner, "start_process_group", return_value=Mock(pid=123)), \
                 patch.object(runner, "stop_process_group"), \
                 patch.object(runner, "wait_for"), patch.object(runner, "free_port", return_value=1234), \
                 patch.object(runner.os, "sched_getaffinity", return_value={0, 1, 2, 3}), \
                 patch.object(runner, "run_process_group", side_effect=self.fake_sample), \
                 patch("builtins.print"):
                for mode in ("unknown", "release", "debug"):
                    output = root / mode
                    reports = (runner.run_experiments(binary, output) if mode == "unknown" else
                               runner.run_experiments(binary, output, mode))
                    self.assertEqual(len(reports), 6)
                    for path in reports:
                        report = json.loads(path.read_text())
                        label = report["benchmark"]["label"]
                        self.assertNotIn(label, labels)
                        labels.add(label)
                        self.assertEqual(report["benchmark"]["server"]["build_mode"], mode)

    def test_failed_group_cleanup_preserves_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "nginx"
            binary.with_suffix(".conf").write_text("")
            output = root / "evidence"
            with patch.object(runner, "start_process_group", return_value=Mock(pid=123)), \
                 patch.object(runner, "stop_process_group", side_effect=PermissionError("denied")), \
                 patch.object(runner, "wait_for", side_effect=RuntimeError("startup failed")), \
                 patch.object(runner, "free_port", return_value=1234), \
                 patch.object(runner.os, "sched_getaffinity", return_value={0, 1, 2, 3}):
                with self.assertRaises(PermissionError):
                    runner.run_experiments(binary, output)
            self.assertTrue((output / "nginx.conf").exists())
