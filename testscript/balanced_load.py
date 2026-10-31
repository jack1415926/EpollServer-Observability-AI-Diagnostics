"""Linux-only, preconnected Ping load with verified per-Worker socket quotas."""
import collections
import concurrent.futures
import json
import math
import multiprocessing
import os
from fractions import Fraction
from pathlib import Path
import socket
import struct
import time

from protocol import require


def process_identity(pid, proc=Path('/proc')):
    fields = (proc / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    return int(fields[19]), int(fields[1])


def check_identities(context, proc=Path('/proc')):
    master = context['master_pid']
    require(process_identity(master, proc)[0] == context['master_start_ticks'], 'Master identity changed')
    for worker in context['workers']:
        require(process_identity(worker['pid'], proc) == (worker['start_ticks'], master), 'Worker identity changed')


def tcp_endpoint(value):
    address, port = value.split(':')
    return socket.inet_ntoa(struct.pack('=I', int(address, 16))), int(port, 16)


def socket_owners(context, endpoints, proc=Path('/proc')):
    """Map exact established IPv4 tuples to accepted FDs of verified Workers."""
    check_identities(context, proc)
    inodes = collections.defaultdict(set)
    for worker in context['workers']:
        for fd in (proc / str(worker['pid']) / 'fd').iterdir():
            try:
                target = os.readlink(fd)
            except FileNotFoundError:
                continue
            if target.startswith('socket:[') and target.endswith(']'):
                inode = target[8:-1]
                inodes[inode].add(worker['pid'])
    result = {}
    for line in (proc / str(context['master_pid']) / 'net/tcp').read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != '01':
            continue
        server, client = tcp_endpoint(fields[1]), tcp_endpoint(fields[2])
        key = (*client, *server)
        if key in endpoints and fields[9] in inodes:
            require(len(inodes[fields[9]]) == 1, 'ambiguous accepted socket ownership')
            require(key not in result, 'duplicate established TCP tuple')
            result[key] = next(iter(inodes[fields[9]]))
    check_identities(context, proc)
    return result


def allocation_bounds(concurrency, workers, tolerance_percent=0):
    require(workers > 0 and concurrency >= workers, 'concurrency must cover every Worker')
    require(math.isfinite(tolerance_percent) and 0 <= tolerance_percent < 100,
            'balance tolerance must be finite and in [0, 100) percent')
    target = Fraction(concurrency, workers)
    tolerance = Fraction(str(tolerance_percent)) / 100
    lower = max(1, math.ceil(target * (1 - tolerance)))
    upper = min(concurrency, math.floor(target * (1 + tolerance)))
    require(lower * workers <= concurrency <= upper * workers,
            'no feasible integer allocation within tolerance; increase tolerance or change concurrency')
    return {'target_connections_per_worker': float(target), 'tolerance_percent': tolerance_percent,
            'min_connections_per_worker': lower, 'max_connections_per_worker': upper}


def prepare_connections(args, context):
    from tcp_stress_test import configure_connected_socket
    check_identities(context)
    workers = [w['pid'] for w in context['workers']]
    require(len(set(workers)) == len(workers), 'Workers must be distinct')
    bounds = allocation_bounds(args.concurrency, len(workers), getattr(args, 'balance_tolerance_percent', 0))
    lower, upper = bounds['min_connections_per_worker'], bounds['max_connections_per_worker']
    counts = collections.Counter()
    retained, pending = [], []
    attempts, deadline = 0, time.monotonic() + 30
    try:
        while len(retained) < args.concurrency:
            require(time.monotonic() < deadline and attempts < args.concurrency * 8,
                    'balanced connection allocation timed out or exceeded candidate limit')
            batch = min(args.concurrency, 2 * (args.concurrency - len(retained)), args.concurrency * 8 - attempts)
            for _ in range(batch):
                require(time.monotonic() < deadline, 'connection allocation deadline exceeded')
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                pending.append({'socket': sock})
                sock.settimeout(min(args.timeout, max(0.01, deadline - time.monotonic())))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if args.source_ip:
                    sock.bind((args.source_ip, 0))
                sock.connect((args.host, args.port))
                pending[-1]['endpoint'] = (*sock.getsockname(), *sock.getpeername())
                configure_connected_socket(sock, args.timeout, args.socket_mode)
                attempts += 1
            keys = {item['endpoint'] for item in pending}
            owners = {}
            while set(owners) != keys:
                require(time.monotonic() < deadline, 'accepted socket ownership unavailable before deadline')
                owners = socket_owners(context, keys)
                if set(owners) != keys:
                    time.sleep(0.01)
            for item in pending:
                pid = owners[item['endpoint']]
                # Reserve enough remaining slots to bring every other Worker to its lower bound.
                deficits = sum(max(0, lower - counts[w] - (w == pid)) for w in workers)
                if (len(retained) < args.concurrency and counts[pid] < upper and
                        args.concurrency - len(retained) - 1 >= deficits):
                    item['worker_pid'] = pid
                    retained.append(item)
                    counts[pid] += 1
                else:
                    item['socket'].close()
            pending = []
        return retained, {'candidate_connections': attempts, 'discarded_connections': attempts - len(retained),
                          **bounds, 'quota_per_worker': lower if lower == upper else None,
                          'preparation_verified_counts': dict(counts)}
    except BaseException:
        for item in [*pending, *retained]:
            item['socket'].close()
        raise


def verified_counts(context, connections):
    expected = {item['endpoint']: item['worker_pid'] for item in connections}
    actual = socket_owners(context, set(expected))
    require(actual == expected, 'retained connection ownership changed or disappeared')
    return dict(collections.Counter(actual.values()))


def client_process(connections, indices, args, barrier, release, output):
    from tcp_stress_test import run_connected
    assigned = set(indices)
    for index, item in enumerate(connections):
        if index not in assigned:
            item['socket'].close()

    def work(index):
        item = connections[index]
        try:
            barrier.wait(timeout=30)
            warmup = run_connected(item['socket'], args.warmup_per_client, 'off', 100,
                                   args.receive_mode, args.validation_mode)
            require(warmup[1] == args.warmup_per_client and not warmup[3], 'balanced warmup failed')
            barrier.wait(timeout=90)
            barrier.wait(timeout=30)
            started = time.perf_counter()
            result = run_connected(item['socket'], args.requests_per_client, args.latency_mode,
                                   args.latency_sample_every, args.receive_mode, args.validation_mode)
            barrier.wait(timeout=300)
            require(release.wait(timeout=30), 'controller did not release retained sockets')
            return {'worker_pid': item['worker_pid'], 'endpoint': item['endpoint'],
                    'started_perf_counter': started, 'warmup_successful': warmup[1], 'result': result}
        except BaseException:
            barrier.abort()
            raise
        finally:
            item['socket'].close()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(indices)) as executor:
            rows = list(executor.map(work, indices))
        output.put({'pid': os.getpid(), 'rows': rows})
    except BaseException as exc:
        barrier.abort()
        output.put({'pid': os.getpid(), 'error': type(exc).__name__ + ': ' + str(exc)})


def run_balanced(args):
    context = json.loads(args.balance_context.read_text())
    require(context['tcp_port'] == args.port and len(context['workers']) == args.server_workers,
            'balanced context does not match target port/Worker count')
    connections, prepared = prepare_connections(args, context)
    mp = multiprocessing.get_context('fork')
    barrier = mp.Barrier(args.concurrency + 1)
    release, output = mp.Event(), mp.Queue()
    processes = []
    stage = 'client_startup'
    method = 'verified_worker_range_v1' if prepared['tolerance_percent'] else 'verified_worker_quota_v1'
    evidence = {'method': method, **prepared, 'master_pid': context['master_pid'],
                'master_start_ticks': context['master_start_ticks'], 'verified_counts': {}}
    try:
        evidence['verified_counts']['before_warmup'] = verified_counts(context, connections)
        for index in range(args.processes):
            indices = list(range(index, len(connections), args.processes))
            process = mp.Process(target=client_process, args=(connections, indices, args, barrier, release, output))
            process.start()
            processes.append(process)
        # The six clients retain their assigned inherited FDs; the controller keeps no extra socket owners.
        for item in connections:
            item['socket'].close()
        barrier.wait(timeout=30)
        stage = 'warmup'
        barrier.wait(timeout=90)
        evidence['verified_counts']['after_warmup'] = verified_counts(context, connections)
        stage = 'measurement'
        evidence['measurement_started_unix_ms'] = time.time_ns() // 1000000
        evidence['measurement_started_boottime_sec'] = time.clock_gettime(time.CLOCK_BOOTTIME)
        started = time.perf_counter()
        barrier.wait(timeout=30)
        barrier.wait(timeout=300)
        duration = time.perf_counter() - started
        evidence['measurement_finished_boottime_sec'] = time.clock_gettime(time.CLOCK_BOOTTIME)
        evidence['measurement_finished_unix_ms'] = time.time_ns() // 1000000
        evidence['verified_counts']['after_measurement'] = verified_counts(context, connections)
        release.set()
        stage = 'result_collection'
        reports = [output.get(timeout=15) for _ in processes]
        failures = [report.get('error') for report in reports if 'rows' not in report]
        require(not failures, 'balanced client failed: ' + str(failures))
        groups, per_worker, starts = [], {}, []
        for report in reports:
            attempted = successful = 0
            latencies, errors = [], collections.Counter()
            for row in report['rows']:
                result = row['result']
                attempted += result[0]
                successful += result[1]
                latencies.extend(result[2])
                errors.update(result[3])
                starts.append(row['started_perf_counter'])
                worker = per_worker.setdefault(str(row['worker_pid']), {'connections': 0, 'attempted': 0,
                                               'successful': 0, 'failed': 0, 'warmup_successful': 0})
                worker['connections'] += 1
                worker['attempted'] += result[0]
                worker['successful'] += result[1]
                worker['failed'] += sum(result[3].values())
                worker['warmup_successful'] += row['warmup_successful']
            groups.append((attempted, successful, latencies, errors))
        evidence.update(workers=per_worker, worker_identities=context['workers'],
                        client_process_pids=[p.pid for p in processes],
                        client_start_skew_ms=(max(starts) - min(starts)) * 1000,
                        warmup_per_client=args.warmup_per_client,
                        connections=[{'endpoint': item['endpoint'], 'worker_pid': item['worker_pid']} for item in connections])
        return groups, duration, evidence
    except BaseException as exc:
        failure = {**evidence, 'failed_stage': stage, 'error': type(exc).__name__ + ': ' + str(exc)}
        if args.json_out:
            args.json_out.with_suffix('.allocation-failure.json').write_text(json.dumps(failure, indent=2) + '\n')
        raise ValueError('balanced load failed during ' + stage + ': ' + type(exc).__name__) from exc
    finally:
        barrier.abort()
        release.set()
        for item in connections:
            item['socket'].close()
        for process in processes:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            require(not process.is_alive(), f'balanced client remains: {process.pid}')
        output.close()
        output.join_thread()
