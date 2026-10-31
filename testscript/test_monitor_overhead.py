"""Workload identity, acceptance arithmetic and failure retention; no performance load."""
from pathlib import Path
import copy
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import benchmark_monitor_overhead as benchmark


class MonitorOverheadTests(unittest.TestCase):
    def test_range_acceptance_checks_all_phases_and_actual_request_counts(self):
        counts = {'1': 49, '2': 51, '3': 50, '4': 50}
        metadata = {'workers': [{'pid': int(pid)} for pid in counts]}
        allocation = {**benchmark.allocation_bounds(200, 4, 2), 'preparation_verified_counts': counts,
                      'verified_counts': {phase: dict(counts) for phase in
                                         ('before_warmup', 'after_warmup', 'after_measurement')},
                      'workers': {pid: {'connections': n, 'attempted': n * 10, 'successful': n * 10,
                                        'failed': 0, 'warmup_successful': n * 3} for pid, n in counts.items()}}
        benchmark.validate_allocation(allocation, metadata, 10, 3, 2)
        for defect in ('changed', 'missing', 'requests', 'warmup', 'bounds'):
            broken = copy.deepcopy(allocation)
            if defect == 'changed':
                broken['verified_counts']['after_measurement'].update({'1': 50, '3': 49})
            elif defect == 'missing':
                del broken['verified_counts']['after_warmup']
            elif defect == 'requests':
                broken['workers']['1']['successful'] = 500
            elif defect == 'warmup':
                broken['workers']['1']['warmup_successful'] -= 1
            else:
                broken['max_connections_per_worker'] = 52
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                benchmark.validate_allocation(broken, metadata, 10, 3, 2)

    def test_matched_config_only_changes_snapshot_switch(self):
        source = 'MysqlPassword = retained-secret\nWorkerProcesses = 1\n'
        configs = [benchmark.configure(source, 18080, mode) for mode in ('off', 'snapshot', 'full', 'collector', 'events')]
        self.assertEqual(configs[1], configs[2])
        self.assertEqual(configs[1], configs[3])
        self.assertEqual(configs[1], configs[4])
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

    def test_collector_removes_database_config_without_changing_shared_environment(self):
        env = {'EPOLL_MONITOR_DB_NAME': 'private', 'EPOLL_MONITOR_DB_PASSWORD': 'secret',
               'EPOLL_MONITOR_TOKEN': 'token', 'PATH': 'path'}
        self.assertEqual(benchmark.monitor_environment(env, 'collector'),
                         {'EPOLL_MONITOR_TOKEN': 'token', 'PATH': 'path'})
        self.assertEqual(benchmark.monitor_environment(env, 'events'), env)
        self.assertEqual(env['EPOLL_MONITOR_DB_NAME'], 'private')

    def test_collector_must_prove_database_disabled_and_other_modes_running(self):
        workers = {'validity': 'complete', 'data': {'workers': [{'resources': {'cpu_percent': 0}}]}}
        disabled = (503, {'code': 'event_store_disabled'}, {})
        running = (200, {'data': {'detection': {'status': 'running', 'store_status': 'ok'}}}, {})
        for mode, reply in [('collector', disabled), ('events', running), ('full', running)]:
            with self.subTest(mode=mode), patch.object(benchmark, 'request', side_effect=[(200, workers, {}), reply]):
                self.assertIn('workers', benchmark.observe_monitor(12345, mode))
        with patch.object(benchmark, 'request', side_effect=[(200, workers, {}), running]):
            with self.assertRaisesRegex(ValueError, 'not disabled'):
                benchmark.observe_monitor(12345, 'collector')

    def test_no_polling_modes_only_sample_process_resources(self):
        for mode in ('snapshot', 'collector', 'events'):
            stopped = threading.Event()
            records, errors = [], []
            with self.subTest(mode=mode), patch.object(benchmark.os, 'sched_setaffinity'), \
                 patch.object(benchmark, 'sample_processes', return_value=([], {})), \
                 patch.object(benchmark, 'request') as request, \
                 patch.object(stopped, 'wait', side_effect=lambda _: stopped.set()):
                benchmark.poll_monitor(12345, Mock(pid=1), Mock(), range(4, 12), stopped,
                                       records, errors, {'phase': 'measured'}, mode)
                request.assert_not_called()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['responses'], {})
                self.assertEqual(errors, [])

    def test_exploration_is_not_formal_acceptance(self):
        result = benchmark.exploration_summary(self.runs())
        self.assertIsNone(result['acceptance_verdict'])
        self.assertNotIn('within_target', result['groups']['full'])
        runs = self.runs()
        runs[0]['successful'] = 20000
        with self.assertRaises(ValueError):
            benchmark.exploration_summary(runs)


if __name__ == '__main__':
    unittest.main()
