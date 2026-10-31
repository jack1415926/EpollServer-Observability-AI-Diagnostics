"""Socket ownership and balanced-load failure boundaries, including real Linux FDs."""
import multiprocessing
import os
import json
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import balanced_load
from balanced_load import allocation_bounds, process_identity, socket_owners, run_balanced
from protocol import recv_pkg, send_pkg


def hold_connections(listener):
    held = []
    while True:
        held.append(listener.accept()[0])


def echo_worker(listener, bad_response):
    def serve(conn):
        with conn:
            while True:
                try:
                    recv_pkg(conn)
                    send_pkg(conn, 5 if bad_response else 0, b'')
                except (OSError, ConnectionError):
                    return
    while True:
        conn = listener.accept()[0]
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


class BalancedLoadTests(unittest.TestCase):
    def test_integer_bounds_and_infeasible_nondivisible_loads(self):
        for concurrency, workers, tolerance, limits in [(200, 4, 2, (49, 51)), (400, 4, 2, (98, 102)),
                                                       (200, 4, 0, (50, 50)), (201, 4, 2, (50, 51)),
                                                       (9, 2, 12, (4, 5))]:
            bounds = allocation_bounds(concurrency, workers, tolerance)
            self.assertEqual((bounds['min_connections_per_worker'], bounds['max_connections_per_worker']), limits)
        for concurrency, workers, tolerance in [(201, 4, 0), (9, 2, 2), (2, 4, 2), (200, 4, -1),
                                                 (200, 4, 100), (200, 4, float('nan')), (200, 4, float('inf'))]:
            with self.subTest(concurrency=concurrency, tolerance=tolerance), self.assertRaises(ValueError):
                allocation_bounds(concurrency, workers, tolerance)

    def test_range_preparation_reserves_lower_bounds_and_closes_excess(self):
        sockets = []
        def create(*_):
            sock = Mock()
            sock.getsockname.return_value = ('127.0.0.1', 40000 + len(sockets))
            sock.getpeername.return_value = ('127.0.0.1', 18000)
            sockets.append(sock)
            return sock
        def owners(_, keys):
            return {key: 11 + min((key[1] - 40000) // 60, 3) for key in keys}
        args = SimpleNamespace(concurrency=200, balance_tolerance_percent=2, host='127.0.0.1', port=18000,
                               timeout=1, source_ip=None, socket_mode='kernel-timeout')
        context = {'workers': [{'pid': pid} for pid in range(11, 15)]}
        with patch.object(balanced_load, 'check_identities'), patch.object(balanced_load.socket, 'socket', side_effect=create), \
             patch.object(balanced_load, 'socket_owners', side_effect=owners), patch('tcp_stress_test.configure_connected_socket'):
            retained, evidence = balanced_load.prepare_connections(args, context)
        try:
            self.assertEqual(len(retained), 200)
            self.assertEqual(evidence['preparation_verified_counts'], {11: 51, 12: 51, 13: 49, 14: 49})
            held = {id(item['socket']) for item in retained}
            self.assertTrue(all(not s.close.called if id(s) in held else s.close.called for s in sockets))
        finally:
            for item in retained:
                item['socket'].close()

    def write_process(self, root, pid, parent, ticks):
        directory = root / str(pid)
        directory.mkdir()
        (directory / 'fd').mkdir()
        fields = ['0'] * 20
        fields[0], fields[1], fields[19] = 'S', str(parent), str(ticks)
        (directory / 'stat').write_text(f'{pid} (test worker) ' + ' '.join(fields))

    def test_exact_tuple_and_identity_are_required(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.write_process(root, 10, 1, 100)
            self.write_process(root, 11, 10, 101)
            (root / '10/net').mkdir()
            (root / '10/net/tcp').write_text('header\n0: 0100007F:4650 0103007F:C350 01 0 0 0 1000 0 77\n')
            (root / '11/fd/3').symlink_to('socket:[77]')
            context = {'master_pid': 10, 'master_start_ticks': 100, 'workers': [{'pid': 11, 'start_ticks': 101}]}
            endpoint = ('127.0.3.1', 50000, '127.0.0.1', 18000)
            self.assertEqual(socket_owners(context, {endpoint}, root), {endpoint: 11})
            self.assertEqual(socket_owners(context, {('127.0.3.2', 50000, '127.0.0.1', 18000)}, root), {})
            context['workers'][0]['start_ticks'] = 999
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                socket_owners(context, {endpoint}, root)

    def test_real_established_sockets_map_to_owned_worker_fds(self):
        context = multiprocessing.get_context('fork')
        processes, clients = [], []
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(32)
            try:
                for _ in range(2):
                    process = context.Process(target=hold_connections, args=(listener,))
                    process.start()
                    processes.append(process)
                master = {'master_pid': os.getpid(), 'master_start_ticks': process_identity(os.getpid())[0],
                          'workers': [{'pid': p.pid, 'start_ticks': process_identity(p.pid)[0]} for p in processes]}
                endpoints = set()
                for _ in range(8):
                    client = socket.create_connection(listener.getsockname(), timeout=2)
                    clients.append(client)
                    endpoints.add((*client.getsockname(), *client.getpeername()))
                deadline = time.monotonic() + 3
                owners = {}
                while len(owners) != 8 and time.monotonic() < deadline:
                    owners = socket_owners(master, endpoints)
                    time.sleep(0.01)
                self.assertEqual(set(owners), endpoints)
                self.assertLessEqual(set(owners.values()), {p.pid for p in processes})
            finally:
                for client in clients:
                    client.close()
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=3)
                    self.assertFalse(process.is_alive())

    def run_real_balanced(self, bad_response=False, concurrency=8, tolerance=0):
        mp = multiprocessing.get_context('fork')
        processes = []
        previous_children = {p.pid for p in mp.active_children()}
        folder = Path(tempfile.mkdtemp(prefix='epoll-balanced-test-'))
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(64)
            try:
                for _ in range(2):
                    process = mp.Process(target=echo_worker, args=(listener, bad_response))
                    process.start()
                    processes.append(process)
                context = {'master_pid': os.getpid(), 'master_start_ticks': process_identity(os.getpid())[0],
                           'tcp_port': listener.getsockname()[1],
                           'workers': [{'pid': p.pid, 'start_ticks': process_identity(p.pid)[0]} for p in processes]}
                path = folder / 'context.json'
                path.write_text(json.dumps(context))
                args = SimpleNamespace(balance_context=path, concurrency=concurrency, processes=2, server_workers=2,
                                       balance_tolerance_percent=tolerance,
                                       host='127.0.0.1', port=context['tcp_port'], source_ip=None, timeout=1,
                                       socket_mode='kernel-timeout', receive_mode='into', validation_mode='strict',
                                       warmup_per_client=3, requests_per_client=10, latency_mode='sampled',
                                       latency_sample_every=2, json_out=Path(folder) / 'result.json')
                if bad_response:
                    with self.assertRaisesRegex(ValueError, 'balanced load failed'):
                        run_balanced(args)
                    failure = json.loads(args.json_out.with_suffix('.allocation-failure.json').read_text())
                    self.assertNotIn('measurement_started_unix_ms', failure)
                else:
                    groups, duration, allocation = run_balanced(args)
                    self.assertGreater(duration, 0)
                    self.assertEqual(sum(g[1] for g in groups), concurrency * 10)
                    self.assertEqual(sum(len(g[2]) for g in groups), concurrency * 5)
                    self.assertTrue(all(not g[3] for g in groups))
                    expected = [4, 5] if concurrency == 9 else [4, 4]
                    self.assertTrue(all(sorted(counts.values()) == expected for counts in allocation['verified_counts'].values()))
                    self.assertEqual(allocation['method'], 'verified_worker_range_v1' if tolerance else 'verified_worker_quota_v1')
                    self.assertEqual(len(allocation['client_process_pids']), 2)
                    self.assertTrue(all(w['successful'] == w['connections'] * 10 and w['warmup_successful'] == w['connections'] * 3
                                        for w in allocation['workers'].values()))
                self.assertEqual({p.pid for p in mp.active_children()}, {p.pid for p in processes})
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=3)
                    self.assertFalse(process.is_alive())
                remaining = {p.pid for p in mp.active_children()} - previous_children
                self.assertFalse(remaining, f'client remains; preserve {folder}: {remaining}')
                shutil.rmtree(folder)

    def test_real_balanced_warmup_barriers_and_per_worker_counts(self):
        self.run_real_balanced()

    def test_real_range_mode_with_nondivisible_concurrency(self):
        self.run_real_balanced(concurrency=9, tolerance=12)

    def test_bad_warmup_aborts_before_measurement_and_reaps_clients(self):
        self.run_real_balanced(bad_response=True)

    def test_unfillable_quota_is_bounded_and_closes_all_candidates(self):
        sockets = []
        def create(*_):
            sock = Mock()
            sock.getsockname.return_value = ('127.0.0.1', 40000 + len(sockets))
            sock.getpeername.return_value = ('127.0.0.1', 18000)
            sockets.append(sock)
            return sock
        args = SimpleNamespace(concurrency=4, host='127.0.0.1', port=18000, timeout=1,
                               source_ip=None, socket_mode='kernel-timeout')
        context = {'workers': [{'pid': 11}, {'pid': 12}]}
        with patch.object(balanced_load, 'check_identities'), patch.object(balanced_load.socket, 'socket', side_effect=create), \
             patch.object(balanced_load, 'socket_owners', side_effect=lambda _, keys: {key: 11 for key in keys}), \
             patch('tcp_stress_test.configure_connected_socket'):
            with self.assertRaisesRegex(ValueError, 'candidate limit'):
                balanced_load.prepare_connections(args, context)
        self.assertEqual(len(sockets), 32)
        self.assertTrue(all(sock.close.called for sock in sockets))


if __name__ == '__main__':
    unittest.main()
