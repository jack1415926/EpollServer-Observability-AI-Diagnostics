"""Retain main process_resources regression alongside the T7 interval sampler."""
import os, signal, json, time, unittest, subprocess
from pathlib import Path
from testscript import test_metrics_collector as fixtures

class ProcessResourcesTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.MetricsCollectorTests.setUpClass.__func__)
    setUp = fixtures.MetricsCollectorTests.setUp
    write = fixtures.MetricsCollectorTests.write
    now = staticmethod(fixtures.MetricsCollectorTests.now)
    process = fixtures.MetricsCollectorTests.process
    collect = fixtures.MetricsCollectorTests.collect
    assert_partial = fixtures.MetricsCollectorTests.assert_partial

    def resource_process(self, pid=101, ticks=11, parent=100, state='S', user='150', system='25', threads='3', rss='512'):
        fields = {i: '0' for i in range(3, 25)}
        fields.update({3: state, 4: str(parent), 14: user, 15: system, 20: threads, 22: str(ticks), 24: rss})
        (self.proc/str(pid)/'stat').write_text(f'{pid} (resource ) probe) '+
            ' '.join(fields[i] for i in range(3,25))+'\n')

    def test_process_resources_survive_stale_business_snapshot(self):
        self.resource_process(state='T')
        self.samples[0]['sampled_at_boottime_ms'] = self.now()-6000
        self.write(self.worker_files[0], self.samples[0])
        sample = self.collect()
        self.assert_partial(sample, 'stale')
        resource = sample['workers'][0]['process_resources']
        self.assertTrue(resource['identity_verified'])
        self.assertEqual(resource['status'], 'complete')
        self.assertEqual(resource['leader_state'], 'T')
        self.assertEqual(resource['cpu_user_ticks'], 150)
        self.assertEqual(resource['cpu_system_ticks'], 25)
        self.assertEqual(resource['clock_ticks_per_second'], os.sysconf('SC_CLK_TCK'))
        self.assertEqual(resource['rss_bytes'], 512*os.sysconf('SC_PAGE_SIZE'))
        self.assertEqual(resource['threads'], 3)

    def test_process_resources_missing_or_bad_values_remain_null(self):
        resource = self.collect()['workers'][0]['process_resources']
        self.assertEqual(resource['status'], 'partial')
        self.assertIsNone(resource['rss_bytes'])
        for user, rss in [('bad', '-1'), ('1', str(2**64-1))]:
            self.resource_process(user=user, rss=rss)
            result = self.collect()
            self.assertEqual(result['status'], 'complete')
            value = result['workers'][0]['process_resources']
            self.assertIsNone(value['rss_bytes'])
            if user == 'bad': self.assertIsNone(value['cpu_user_ticks'])

    def test_process_resources_reject_reused_identity(self):
        self.resource_process(ticks=999)
        result = self.collect()
        self.assert_partial(result, 'identity_mismatch')
        resource = result['workers'][0]['process_resources']
        self.assertFalse(resource['identity_verified'])
        self.assertIsNone(resource['leader_state'])
        self.assertIsNone(resource['rss_bytes'])

    def test_real_proc_stop_and_resume_with_controlled_snapshot(self):
        from testscript.process_group import start_process_group, stop_process_group
        child = start_process_group(['sleep', '60'])
        self.processes.append(child)
        self.addCleanup(stop_process_group, child, timeout=1)
        def ticks(pid):
            return int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19])
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        parent, parent_ticks, child_ticks = os.getpid(), ticks(os.getpid()), ticks(child.pid)
        member = {'worker_index': 0, 'pid': child.pid, 'process_start_ticks': child_ticks}
        manifest = self.metrics/f'master-{boot}-{parent}-{parent_ticks}.json'
        self.write(manifest, {'schema_version': 1, 'boot_id': boot, 'master_pid': parent,
            'master_start_ticks': parent_ticks, 'sequence': 1, 'write_failures_total': 0,
            'state': 'running', 'expected_workers': 1, 'workers': [member]})
        sample = {**member, 'schema_version': 1, 'boot_id': boot, 'master_pid': parent,
            'master_start_ticks': parent_ticks, 'sequence': 1, 'collection_ok': True,
            'write_failures_total': 0, 'sampled_at_unix_ms': int(time.time()*1000),
            'sampled_at_boottime_ms': self.now()-6000, 'connections_current': 0,
            'receive_queue_messages': 0, 'send_queue_messages': 0}
        path = self.metrics/f'worker-{boot}-{child.pid}-{child_ticks}.json'
        self.write(path, sample)
        def actual():
            return json.loads(subprocess.check_output([str(self.binary), str(manifest), '3000', '/proc'], text=True, timeout=5))
        self.assertEqual(ticks(child.pid), child_ticks)
        os.kill(child.pid, signal.SIGSTOP)
        try:
            deadline = time.monotonic()+3
            while Path(f'/proc/{child.pid}/stat').read_text().rsplit(')',1)[1].split()[0] != 'T':
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            result = actual()
            self.assertEqual(result['workers'][0]['status'], 'stale')
            resource = result['workers'][0]['process_resources']
            self.assertEqual(resource['leader_state'], 'T')
            self.assertTrue(resource['identity_verified'])
            self.assertIsInstance(resource['rss_bytes'], int)
        finally:
            self.assertEqual(ticks(child.pid), child_ticks)
            os.kill(child.pid, signal.SIGCONT)
        sample['sampled_at_boottime_ms'] = self.now()
        self.write(path, sample)
        self.assertEqual(actual()['workers'][0]['status'], 'fresh')
