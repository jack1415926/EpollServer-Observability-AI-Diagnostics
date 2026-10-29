"""Workload identity, acceptance arithmetic and failure retention; no performance load."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import benchmark_monitor_overhead as benchmark


class MonitorOverheadTests(unittest.TestCase):
    def test_matched_config_only_changes_snapshot_switch(self):
        source = 'MysqlPassword = retained-secret\nWorkerProcesses = 1\n'
        configs = [benchmark.configure(source, 18080, mode) for mode in ('off', 'snapshot', 'full')]
        self.assertEqual(configs[1], configs[2])
        self.assertEqual(configs[0].replace('WorkerSnapshotEnable = 0', 'WorkerSnapshotEnable = 1'), configs[1])
        for config in configs:
            for line in ('WorkerProcesses = 4', 'ProcMsgRecvWorkThreadCount = 120', 'MysqlPoolSize = 16',
                         'RedisRateLimitEnable = 1', 'RedisRateLimitMaxRequests = 100000000',
                         'MysqlPassword = retained-secret'):
                self.assertIn(line, config)

    def runs(self):
        return [{'mode': mode, 'qps': qps, 'latency_ms': {'p95': p95},
                 'successful': 2000000, 'failed': 0, 'not_attempted': 0}
                for mode, qps, p95 in [('off', 100, 10), ('snapshot', 98, 10.5), ('full', 96, 10.9)]
                for _ in range(3)]

    def test_targets_and_medians_are_measured_per_condition(self):
        runs = self.runs()
        groups = benchmark.summarize(runs)
        self.assertTrue(groups['full']['within_target'])
        self.assertAlmostEqual(groups['full']['qps_change_percent'], -4)
        self.assertAlmostEqual(groups['full']['p95_change_percent'], 9)
        for run in runs:
            if run['mode'] == 'full':
                run['qps'], run['latency_ms']['p95'] = 95, 11
        self.assertTrue(benchmark.summarize(runs)['full']['within_target'])
        for run in runs:
            if run['mode'] == 'full':
                run['qps'] = 94
        self.assertFalse(benchmark.summarize(runs)['full']['within_target'])

    def test_failures_and_short_runs_cannot_be_formal_acceptance(self):
        for field, value in [('successful', 20000), ('failed', 1), ('not_attempted', 1)]:
            runs = self.runs()
            runs[0][field] = value
            with self.assertRaises(ValueError):
                benchmark.summarize(runs)
        with self.assertRaises(ValueError):
            benchmark.summarize(self.runs()[:-1])

    def test_cleanup_failure_or_occupied_port_keeps_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'nginx.conf'
            config.write_text('private')
            process = Mock()
            with patch.object(benchmark, 'stop_process_group', side_effect=RuntimeError('still alive')):
                with self.assertRaises(RuntimeError):
                    benchmark.stop_owned(process, [12345], config)
            self.assertTrue(config.exists())
            with patch.object(benchmark, 'stop_process_group'), patch.object(benchmark, 'closed', return_value=False):
                with self.assertRaises(ValueError):
                    benchmark.stop_owned(process, [12345], config)
            self.assertTrue(config.exists())
            with patch.object(benchmark, 'stop_process_group'), patch.object(benchmark, 'closed', return_value=True):
                benchmark.stop_owned(process, [12345], config)
            self.assertFalse(config.exists())


if __name__ == '__main__':
    unittest.main()
