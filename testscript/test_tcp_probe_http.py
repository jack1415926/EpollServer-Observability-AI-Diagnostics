"""Cached TCP probe -> real Drogon HTTP -> production diagnosis tool, without a database."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from diagnosis.agent import Tools
from process_group import start_process_group, stop_process_group, remove_process_directory
from test_http_monitor import TOKEN, free_port, identity, request, wait_for

ROOT = Path(__file__).resolve().parents[1]
PACKET = b'\x00\x08\x00\x00\x00\x00\x00\x00'


class TcpProbeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(['make', '-C', str(ROOT/'monitor'), 'http'], check=True, timeout=120)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='epoll-tcp-probe-http-'))
        self.processes = []
        self.addCleanup(remove_process_directory, self.root, self.processes)
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0)); self.listener.listen(); self.listener.settimeout(.05)
        self.tcp_port = self.listener.getsockname()[1]
        self.mode = 'ok'
        self.stopped = threading.Event()
        self.accepted = 0
        def serve():
            while not self.stopped.is_set():
                try:
                    conn, _ = self.listener.accept()
                except socket.timeout:
                    continue
                with conn:
                    self.accepted += 1
                    conn.settimeout(.3)
                    try:
                        received = b''
                        while len(received) < 8:
                            chunk = conn.recv(8-len(received))
                            if not chunk: break
                            received += chunk
                        if self.mode == 'timeout':
                            self.stopped.wait(.25)
                        elif self.mode == 'bad':
                            conn.sendall(PACKET[:-1] + b'\x01')
                        else:
                            conn.sendall(PACKET)
                    except OSError:
                        pass
        self.thread = threading.Thread(target=serve)
        self.thread.start()
        def stop_listener():
            self.stopped.set(); self.thread.join(timeout=2)
            self.assertFalse(self.thread.is_alive())
            self.listener.close()
        self.addCleanup(stop_listener)
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        ticks = identity(os.getpid())
        self.manifest = self.root/f'master-{boot}-{os.getpid()}-{ticks}.json'
        self.master = {'schema_version':1, 'boot_id':boot, 'master_pid':os.getpid(), 'master_start_ticks':ticks,
                       'sequence':1, 'write_failures_total':0, 'state':'running', 'expected_workers':0, 'workers':[]}
        self.manifest.write_text(json.dumps(self.master)); self.manifest.chmod(0o600)
        self.http_port = free_port()
        self.log = (self.root/'http.log').open('wb')
        self.addCleanup(self.log.close)
        env = {k:v for k,v in os.environ.items() if not k.startswith(('EPOLL_MONITOR_', 'EPOLL_DIAG_'))}
        env.update(EPOLL_MONITOR_TOKEN=TOKEN, EPOLL_MONITOR_TCP_PORT=str(self.tcp_port),
                   EPOLL_MONITOR_TCP_INTERVAL_MS='400', EPOLL_MONITOR_TCP_TIMEOUT_MS='80')
        self.monitor = start_process_group([str(ROOT/'monitor/build/epoll-monitor'), str(self.manifest), str(self.http_port)],
                                           env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.processes.append(self.monitor)
        self.addCleanup(stop_process_group, self.monitor, timeout=3)
        wait_for(lambda: self.health_matches('ping_ok'))

    def health_matches(self, reason):
        self.assertIsNone(self.monitor.poll(), (self.root/'http.log').read_text())
        try:
            code, result, _ = request(self.http_port, 'health')
            return code == 200 and result['data']['tcp_probe']['reason'] == reason and result
        except OSError:
            return False

    def test_failures_recovery_and_fast_http_cached_reads(self):
        baseline = self.accepted
        for _ in range(5):
            code, value, _ = request(self.http_port, 'health')
            self.assertEqual(code, 200)
            self.assertEqual(value['data']['monitor'], 'ok')
        self.assertLessEqual(self.accepted-baseline, 1)
        for mode, reason in [('timeout','timeout'), ('bad','protocol_error'), ('ok','ping_ok')]:
            self.mode = mode
            value = wait_for(lambda: self.health_matches(reason))
            self.assertEqual(value['data']['tcp_health'], 'reachable' if mode=='ok' else 'unreachable')
            started = time.monotonic()
            self.assertEqual(request(self.http_port, 'health')[0], 200)
            self.assertLess(time.monotonic()-started, .5)
        for endpoint in ('overview','workers','metrics'):
            _, value, _ = request(self.http_port, endpoint)
            self.assertEqual(value['data']['tcp_probe']['status'], 'reachable')
            self.assertEqual(value['data']['tcp_health'], 'reachable')

    def test_production_diagnosis_tool_preserves_probe_evidence(self):
        async def read():
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{self.http_port}',
                                        headers={'Authorization':'Bearer '+TOKEN}, trust_env=False) as client:
                tools = Tools(client, None)
                result = await tools.call('get_server_health', {})
                observed = json.loads(result['excerpt'])['response']['data']
                self.assertEqual(observed['tcp_health'], 'reachable')
                self.assertEqual(observed['tcp_probe']['instance']['master_pid'], os.getpid())
                self.assertEqual(tools.target['master'], observed['tcp_probe']['instance'])
                self.assertGreater(observed['tcp_probe']['rtt_ms'], 0)
                self.assertEqual(result['scope'], 'live_observation')
        with patch.dict(os.environ, {'EPOLL_MONITOR_PORT':str(self.http_port), 'EPOLL_MONITOR_TOKEN':TOKEN}):
            asyncio.run(read())

    def test_instance_change_invalidates_cached_success(self):
        self.master['state'] = 'stopping'
        temporary = self.manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.master)); temporary.chmod(0o600); temporary.replace(self.manifest)
        value = wait_for(lambda: self.health_matches('identity_unavailable'))
        self.assertEqual(value['data']['tcp_health'], 'unknown')
        self.assertIsNone(value['data']['tcp_probe']['rtt_ms'])


if __name__ == '__main__':
    unittest.main()
