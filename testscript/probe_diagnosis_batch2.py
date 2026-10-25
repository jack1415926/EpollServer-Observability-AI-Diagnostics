#!/usr/bin/env python3
"""Real /proc + Drogon observation with controlled snapshots; no TCP/MySQL load.

--online optionally analyzes the captured observation with the new knowledge
index. It is a functional probe, not a quality score or real business incident.
"""
import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diagnosis.agent import diagnose
from diagnosis.config import load_environment
from diagnosis.providers import Providers
from diagnosis.retrieval import Retriever
from evaluation.diagnosis.run import ReplayTools, replay_transport
from process_group import start_process_group, stop_process_group
from protocol import require
from test_http_monitor import TOKEN, free_port, identity, request, wait_for, HttpMonitorTests


async def analyze(observations, directory, index):
    import httpx
    sample = observations['workers']['data']
    replay = {'replies': observations,
        'logs': {'available': False, 'instance_verified': False}, 'comparison': None}
    async with httpx.AsyncClient(trust_env=False, mounts={'http://127.0.0.1': replay_transport(replay)}) as client:
        tools = ReplayTools(client, Retriever(Providers(client), index), replay)
        async with asyncio.timeout(90):
            value = await diagnose('请根据这组已捕获的观测，区分业务快照stale与process_resources中身份已核验的T状态。'
                '指出观测时哪些事实能确认、哪些因果仍需验证；累计CPU时间是否代表当前CPU利用率？', Providers(client), tools)
    value['probe_context'] = {'kind': 'real_proc_with_controlled_snapshot_replay',
        'captured_at_unix_ms': sample['collected_at_unix_ms'],
        'not_a_real_tcp_business_incident': True, 'not_a_quality_score': True}
    (directory/'diagnosis.json').write_text(json.dumps(value, ensure_ascii=False, indent=2))
    print('PASS: new corpus remote analysis and report contract', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--hold', action='store_true')
    parser.add_argument('--index', type=Path, default=ROOT/'artifacts/diagnosis-batch2/index-current.npz')
    args = parser.parse_args()
    if args.hold and not sys.stdin.isatty(): parser.error('--hold requires an interactive terminal')
    require((ROOT/'monitor/build/epoll-monitor').is_file(), 'build monitor http first')
    load_environment()
    if args.online: require(args.index.is_file(), 'build the selected index first')
    parent = ROOT/'artifacts/diagnosis-batch2'; parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='evidence-', dir=parent)); directory.chmod(0o700)
    print(f'Evidence: {directory}', flush=True)
    with ExitStack() as cleanup:
        worker = start_process_group(['sleep', '600'])
        cleanup.callback(stop_process_group, worker, timeout=1)
        ticks, master_ticks = identity(worker.pid), identity(os.getpid())
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        member = {'pid': worker.pid, 'worker_index': 0, 'process_start_ticks': ticks}
        manifest = directory/f'master-{boot}-{os.getpid()}-{master_ticks}.json'
        HttpMonitorTests.write(manifest, {'schema_version': 1, 'boot_id': boot, 'master_pid': os.getpid(),
            'master_start_ticks': master_ticks, 'sequence': 1, 'write_failures_total': 0,
            'state': 'running', 'expected_workers': 1, 'workers': [member]})
        path = directory/f'worker-{boot}-{worker.pid}-{ticks}.json'
        enabled, stopped = threading.Event(), threading.Event(); enabled.set()
        def publish():
            sequence = 0
            while not stopped.is_set():
                if enabled.is_set():
                    sequence += 1
                    HttpMonitorTests.write(path, {**member, 'schema_version': 1, 'boot_id': boot,
                        'master_pid': os.getpid(), 'master_start_ticks': master_ticks, 'sequence': sequence,
                        'write_failures_total': 0, 'collection_ok': True, 'connections_current': 0,
                        'receive_queue_messages': 0, 'send_queue_messages': 0,
                        'sampled_at_unix_ms': int(time.time()*1000),
                        'sampled_at_boottime_ms': int(time.clock_gettime(time.CLOCK_BOOTTIME)*1000)})
                stopped.wait(.1)
        publisher = threading.Thread(target=publish); publisher.start()
        def stop_publisher():
            stopped.set(); publisher.join(timeout=5)
            require(not publisher.is_alive(), 'publisher did not exit')
        cleanup.callback(stop_publisher)
        log = cleanup.enter_context((directory/'monitor.log').open('wb'))
        port = free_port()
        monitor = start_process_group([str(ROOT/'monitor/build/epoll-monitor'), str(manifest), str(port)],
            env={**os.environ, 'EPOLL_MONITOR_TOKEN': TOKEN, 'EPOLL_MONITOR_DB_NAME': ''},
            stdout=log, stderr=subprocess.STDOUT)
        cleanup.callback(stop_process_group, monitor, timeout=2)
        def ready():
            require(monitor.poll() is None, 'monitor exited')
            try: return request(port, 'workers')[1]['validity'] == 'complete'
            except OSError: return False
        wait_for(ready)
        enabled.clear()
        require(identity(worker.pid) == ticks, 'worker identity changed before pause')
        os.kill(worker.pid, signal.SIGSTOP)
        try:
            wait_for(lambda: request(port, 'workers')[1]['data']['workers'][0]['status'] == 'stale', timeout=10)
            captured = request(port, 'workers')[1]['data']
            observations = {key: request(port, key)[1] for key in ('health', 'workers', 'events', 'experiments')}
            value = captured['workers'][0]['process_resources']
            require(value['identity_verified'] and value['leader_state'] == 'T', 'actual stopped state missing')
            require(value['rss_bytes'] is not None and value['cpu_user_ticks'] is not None, 'actual resources missing')
            (directory/'paused-observation.json').write_text(json.dumps(captured, indent=2))
            print('PASS: stale controlled snapshot and independently verified real stopped process', flush=True)
        finally:
            require(identity(worker.pid) == ticks, 'worker identity changed before resume')
            os.kill(worker.pid, signal.SIGCONT); enabled.set()
        wait_for(lambda: request(port, 'workers')[1]['validity'] == 'complete')
        (directory/'resumed-observation.json').write_text(json.dumps(request(port,'workers')[1]['data'],indent=2))
        if args.online: asyncio.run(analyze(observations, directory, args.index))
        if args.hold:
            print(f'Browser QA: http://127.0.0.1:{port}/#workers ; token is test_http_monitor.TOKEN', flush=True)
            input('Press Enter to clean up: ')
    require(worker.group_stopped and monitor.group_stopped, 'process cleanup incomplete')
    print('PASS: owned worker/monitor/publisher exited; evidence retained', flush=True)


if __name__ == '__main__':
    main()
