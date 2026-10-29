#!/usr/bin/env python3
"""Matched off/snapshot/full monitoring experiment; private Redis and monitoring MySQL."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time

from benchmark_worker_snapshot import configuration
from import_experiment import upload
from performance_sampler import process_summary, sample_processes
from process_group import run_process_group, start_process_group, stop_process_group
from protocol import require
from test_http_monitor import TOKEN, free_port, identity, request, wait_for
from test_shutdown import children
from test_stage3_live import TestDatabase

ROOT = Path(__file__).resolve().parents[1]
ORDER = ('off', 'snapshot', 'full', 'full', 'off', 'snapshot', 'snapshot', 'full', 'off')


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configure(source, port, mode):
    overrides = {'WorkerProcesses': 4, 'ProcMsgRecvWorkThreadCount': 120,
                 'MysqlPoolSize': 16, 'worker_connections': 10000}
    lines = []
    for line in configuration(source, port, mode != 'off').splitlines():
        match = re.match(r'\s*([A-Za-z0-9_]+)\s*=', line)
        if not match or match[1] not in overrides:
            lines.append(line)
    return '\n'.join(lines + [f'{k} = {v}' for k, v in overrides.items()]) + '\n'


def summarize(runs):
    groups = {}
    for mode in ('off', 'snapshot', 'full'):
        group = [r for r in runs if r['mode'] == mode]
        require(len(group) == 3, 'each condition requires three measured rounds')
        require(all(r['failed'] == r['not_attempted'] == 0 and r['successful'] == 2000000
                    and r['qps'] > 0 and r['latency_ms']['p95'] > 0 for r in group),
                'formal workload incomplete or failed')
        groups[mode] = {'median_qps': statistics.median(r['qps'] for r in group),
                        'median_sampled_p95_ms': statistics.median(r['latency_ms']['p95'] for r in group),
                        'qps_range': [min(r['qps'] for r in group), max(r['qps'] for r in group)],
                        'sampled_p95_range_ms': [min(r['latency_ms']['p95'] for r in group),
                                                 max(r['latency_ms']['p95'] for r in group)]}
    for mode in ('snapshot', 'full'):
        groups[mode]['qps_change_percent'] = (groups[mode]['median_qps'] / groups['off']['median_qps'] - 1) * 100
        groups[mode]['p95_change_percent'] = (groups[mode]['median_sampled_p95_ms'] / groups['off']['median_sampled_p95_ms'] - 1) * 100
        groups[mode]['within_target'] = (groups[mode]['median_qps'] >= groups['off']['median_qps'] * 0.95 and
                                         groups[mode]['median_sampled_p95_ms'] <= groups['off']['median_sampled_p95_ms'] * 1.10)
    return groups


def closed(port):
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(('127.0.0.1', port)) != 0


def stop_owned(process, ports, config=None):
    stop_process_group(process)
    require(all(closed(port) for port in ports), f'owned ports still occupied: {ports}')
    if config is not None:
        config.unlink(missing_ok=True)


def poll_monitor(port, process, database, client_cpus, stopped, records, errors, state):
    # Pin this polling thread away from the four TCP/monitor CPUs.
    os.sched_setaffinity(0, client_cpus)
    previous, before = {}, time.monotonic()
    while not stopped.is_set():
        try:
            now = time.monotonic()
            resources, previous = sample_processes([('monitor', process.pid), ('monitor_db', database.process.pid)],
                                                  previous, now - before, os.sysconf('SC_CLK_TCK'))
            before = now
            cycle = {'phase': state['phase'], 'boottime_sec': time.clock_gettime(time.CLOCK_BOOTTIME),
                     'processes': resources, 'responses': {}}
            for endpoint in ('overview', 'workers', 'events'):
                code, value, _ = request(port, endpoint)
                require(code == 200, f'{endpoint} HTTP {code}')
                if endpoint in ('overview', 'workers'):
                    require(value['validity'] == 'complete', f'{endpoint} incomplete')
                else:
                    require(value['data']['detection']['status'] == 'running', 'event detector not running')
                cycle['responses'][endpoint] = value
            records.append(cycle)
        except Exception as exc:
            errors.append(str(exc))
            stopped.set()
        stopped.wait(2)


def run_round(args, root, index, mode, environment, database):
    directory = root / f'{index:02d}-{mode}'
    directory.mkdir(mode=0o700)
    port, http_port = free_port(), free_port()
    while http_port == port:
        http_port = free_port()
    config = directory / 'nginx.conf'
    config.write_text(configure(args.binary.with_name('nginx.conf').read_text(), port, mode))
    config.chmod(0o600)
    records, errors, results, state = [], [], {}, {'phase': 'startup'}
    with ExitStack() as cleanup:
        console = cleanup.enter_context((directory / 'tcp.log').open('wb'))
        tcp = start_process_group(['taskset', '-c', '0-3', str(args.binary)], cwd=directory,
                                  stdout=console, stderr=subprocess.STDOUT)
        cleanup.callback(stop_owned, tcp, [port], config)
        def ready():
            require(tcp.poll() is None, f'TCP startup failed: {directory}')
            log = directory / 'server.log'
            return log.exists() and log.read_text(errors='replace').count('【worker进程】启动') == 4
        wait_for(ready, timeout=30)
        workers = children(tcp.pid)
        require(len(workers) == 4, 'expected four owned Workers')
        metadata = {'mode': mode, 'tcp_port': port, 'master_pid': tcp.pid, 'master_start_ticks': identity(tcp.pid),
                    'workers': [{'pid': pid, 'start_ticks': identity(pid)} for pid in workers],
                    'server_cpus': '0-3', 'client_cpus': '4-11', 'monitor_cpus': '0-3' if mode == 'full' else None,
                    'monitor_pid': None, 'commands': {}}
        if mode != 'off':
            def publishers_ready():
                samples = [json.loads(p.read_text()) for p in (directory / 'metrics').glob('worker-*.json')]
                return len(samples) == 4 and all(s['sequence'] >= 2 for s in samples)
            wait_for(publishers_ready, timeout=10)
        if mode == 'full':
            manifest = next((directory / 'metrics').glob('master-*.json'))
            output = cleanup.enter_context((directory / 'http.log').open('wb'))
            monitor = start_process_group(['taskset', '-c', '0-3', str(args.monitor), str(manifest), str(http_port)],
                                          cwd=directory, env=environment, stdout=output, stderr=subprocess.STDOUT)
            cleanup.callback(stop_owned, monitor, [http_port])
            metadata.update(monitor_pid=monitor.pid, monitor_start_ticks=identity(monitor.pid), http_port=http_port)
            def monitor_ready():
                require(monitor.poll() is None, f'monitor startup failed: {directory}')
                try:
                    value = request(http_port, 'workers')[1]
                    return (value['validity'] == 'complete' and
                            all(w['resources']['cpu_percent'] is not None for w in value['data']['workers']) and
                            request(http_port, 'events')[1].get('data', {}).get('detection', {}).get('status') == 'running')
                except OSError:
                    return False
            wait_for(monitor_ready, timeout=20)
            if index <= 3:
                key = upload(f'http://127.0.0.1:{http_port}', json.loads(args.report.read_text()), TOKEN)
                code, stored, _ = request(http_port, f'experiments/{key}')
                require(code == 200, 'real existing experiment write/read failed')
                write(directory / 'import-readback.json', stored)
            stopped = threading.Event()
            thread = threading.Thread(target=poll_monitor, args=(http_port, monitor, database, range(4, 12), stopped, records, errors, state))
            def stop_polling():
                stopped.set()
                thread.join(timeout=12)
                require(not thread.is_alive(), 'polling thread still alive; preserve evidence')
                write(directory / 'http-samples.json', {'samples': records, 'errors': errors,
                      'measured_process_summary': process_summary([r for r in records if r['phase'] == 'measured'])})
            cleanup.callback(stop_polling)
            thread.start()
        for phase, count in (('warmup', 50 if args.smoke else 500), ('measured', 100 if args.smoke else 10000)):
            state['phase'] = phase
            load = ['taskset', '-c', '4-11', sys.executable, str(ROOT / 'testscript/tcp_stress_test.py'),
                    '--port', str(port), '--timeout', '5', '--concurrency', '200', '--processes', '6',
                    '--requests-per-client', str(count), '--source-ip', '127.0.3.1', '--receive-mode', 'into',
                    '--validation-mode', 'strict', '--socket-mode', 'kernel-timeout', '--latency-mode', 'sampled',
                    '--latency-sample-every', '100', '--label', f'{index}-{mode}-{phase}', '--build-mode', 'debug',
                    '--server-workers', '4', '--worker-threads', '120', '--rate-limit-mode', 'enabled',
                    '--json-out', str(directory / f'{phase}.json')]
            command = [sys.executable, str(ROOT / 'testscript/performance_sampler.py'), '--master-pid', str(tcp.pid),
                       '--label', f'{index}-{mode}-{phase}', '--interval', '0.5', '--log', str(directory / 'server.log'),
                       '--output', str(directory / f'{phase}-resources.json'), '--', *load]
            metadata['commands'][phase] = command
            write(directory / 'context.json', metadata)
            with (directory / f'{phase}.log').open('wb') as output:
                run_process_group(command, stdout=output, stderr=subprocess.STDOUT, timeout=300)
            result = json.loads((directory / f'{phase}.json').read_text())['result']
            require(result['successful'] == 200 * count and result['failed'] == result['not_attempted'] == 0,
                    f'request failures: {directory}/{phase}.json')
            require(not errors, f'monitor polling errors: {errors}')
            results[phase] = result
        if mode == 'off':
            require(not (directory / 'metrics').exists(), 'off condition generated snapshots')
        else:
            samples = [json.loads(p.read_text()) for p in (directory / 'metrics').glob('worker-*.json')]
            require(len(samples) == 4 and all(s['sequence'] >= 2 for s in samples), 'publisher did not sample repeatedly')
        if mode == 'full':
            stop_polling()
            require(not errors, f'monitor polling errors: {errors}')
            require(records and all(w['resources']['cpu_percent'] is not None
                    for w in records[-1]['responses']['workers']['data']['workers']), 'no usable CPU/RSS monitoring sample')
        tcp.send_signal(signal.SIGTERM)
        require(tcp.wait(timeout=30) == 0, 'TCP normal shutdown failed')
    require(all(not Path(f'/proc/{pid}').exists() for pid in workers), 'owned Worker remains after cleanup')
    write(directory / 'cleanup.json', {'confirmed': True, 'tcp_port_closed': closed(port),
                                      'config_removed': not config.exists(), 'worker_pids': workers})
    measured = {'mode': mode, 'directory': directory.name, **results['measured']}
    print(f"PASS {directory.name}: QPS={measured['qps']}, P95={measured['latency_ms']['p95']} ms; cleanup confirmed", flush=True)
    time.sleep(1)
    return measured


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=ROOT / 'server/nginx')
    parser.add_argument('--monitor', type=Path, default=ROOT / 'monitor/build/epoll-monitor')
    parser.add_argument('--report', type=Path, required=True, help='existing real experiment report for DB preflight')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts')
    parser.add_argument('--smoke', action='store_true', help='three short conditions; never formal acceptance')
    args = parser.parse_args()
    for key in ('binary', 'monitor', 'report'):
        setattr(args, key, getattr(args, key).resolve(strict=True))
    report = json.loads(args.report.read_text())
    require(isinstance(report.get('benchmark'), dict) and 'result' in report['benchmark'] and
            isinstance(report.get('context'), dict), 'invalid existing experiment report')
    require(set(range(12)).issubset(os.sched_getaffinity(0)), 'requires CPUs 0-11, matching baseline placement')
    require(closed(6379), '6379 already occupied; do not touch existing Redis')
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='monitor-overhead-smoke-' if args.smoke else 'monitor-overhead-formal-', dir=args.output.resolve()))
    root.chmod(0o700)
    local_config_hash = sha(args.binary.with_name('nginx.conf'))
    files = [args.binary, args.monitor, args.report, Path(__file__).resolve(),
             ROOT / 'testscript/tcp_stress_test.py', ROOT / 'testscript/performance_sampler.py',
             ROOT / 'testscript/benchmark_worker_snapshot.py', ROOT / 'testscript/process_group.py',
             ROOT / 'testscript/test_stage3_live.py', ROOT / 'testscript/test_shutdown.py']
    write(root / 'manifest.json', {'scope': 'smoke' if args.smoke else 'formal matched monitoring overhead',
          'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
          'system': platform.platform(), 'python': platform.python_version(),
          'hashes': {str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p): sha(p) for p in files},
          'local_config_sha256': local_config_hash, 'affinity': sorted(os.sched_getaffinity(0)),
          'meminfo_before': Path('/proc/meminfo').read_text(), 'order': list(ORDER[:3] if args.smoke else ORDER),
          'targets': {'qps_drop_percent_max': 5, 'sampled_p95_increase_percent_max': 10, 'request_failures': 0},
          'limitations': ['same-host Debug Ping, no production capacity claim', 'no AI requests or fault-injection load',
                         'Redis/monitoring MySQL inherit runner affinity; TCP/monitor share CPUs 0-3',
                         'three polling endpoints every two seconds, no interactive browser',
                         'historical baseline is context only, not the comparison denominator']})
    print(f'Evidence: {root}', flush=True)
    runs = []
    with ExitStack() as cleanup:
        output = cleanup.enter_context((root / 'redis.log').open('wb'))
        redis = start_process_group(['redis-server', '--bind', '127.0.0.1', '--port', '6379', '--save', '',
                                    '--appendonly', 'no', '--daemonize', 'no', '--dir', str(root)],
                                   stdout=output, stderr=subprocess.STDOUT)
        cleanup.callback(stop_owned, redis, [6379])
        def redis_ready():
            require(redis.poll() is None, 'private Redis failed to start')
            return subprocess.run(['redis-cli', 'PING'], capture_output=True, timeout=3).stdout.strip() == b'PONG'
        wait_for(redis_ready)
        database = TestDatabase(root / 'database')
        environment = database.__enter__()
        cleanup.callback(database.__exit__, None, None, None)
        # Ignore inherited endpoint/threshold overrides to make all rounds reproducible.
        environment = {k: v for k, v in environment.items() if not k.startswith(('EPOLL_DIAG', 'EPOLL_MONITOR_EVENT_',
                       'EPOLL_MONITOR_RECEIVE_', 'EPOLL_MONITOR_SEND_'))}
        subprocess.run(database.admin, input="GRANT SELECT,INSERT,UPDATE ON epoll_monitor_test.monitor_event TO 'monitor_test'@'127.0.0.1';",
                       text=True, capture_output=True, check=True, timeout=10)
        write(root / 'owned-services.json', {'redis_pid': redis.pid, 'redis_start_ticks': identity(redis.pid),
              'redis_port': 6379, 'mysql_pid': database.process.pid, 'mysql_start_ticks': identity(database.process.pid),
              'mysql_port': environment['EPOLL_MONITOR_DB_PORT'], 'mysql_data_root': str(database.root)})
        for index, mode in enumerate(ORDER[:3] if args.smoke else ORDER, 1):
            require(redis.poll() is None and database.process.poll() is None, 'owned dependency exited')
            runs.append(run_round(args, root, index, mode, environment, database))
            write(root / 'runs.json', runs)
    require(sha(args.binary.with_name('nginx.conf')) == local_config_hash, 'local config changed')
    require(closed(int(environment['EPOLL_MONITOR_DB_PORT'])) and not database.root.exists(),
            'private database port/directory cleanup incomplete')
    summary = {'scope': 'smoke' if args.smoke else 'formal', 'runs': runs, 'cleanup_confirmed': True,
               'local_config_unchanged': True, 'groups': None if args.smoke else summarize(runs)}
    write(root / 'summary.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'runs'}, indent=2), flush=True)


if __name__ == '__main__':
    main()
