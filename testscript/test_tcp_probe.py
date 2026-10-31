"""Production C++ Ping probe against real sockets and selected /proc identities."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from test_http_monitor import identity

ROOT = Path(__file__).resolve().parents[1]
PACKET = b'\x00\x08\x00\x00\x00\x00\x00\x00'


class TcpProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix='epoll-tcp-probe-unit-')
        cls.addClassCleanup(cls.folder.cleanup)
        cls.binary = Path(cls.folder.name) / 'probe'
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pthread',
                        '-I' + str(ROOT / '.deps/usr/include'), '-I' + str(ROOT / 'monitor/http'),
                        str(ROOT / 'monitor/http/tcp_probe.cxx'), str(ROOT / 'testscript/tcp_probe_driver.cxx'),
                        '-o', str(cls.binary)], check=True, timeout=60)

    def master(self):
        return {'master_pid': os.getpid(), 'master_start_ticks': identity(os.getpid()), 'state': 'running',
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}

    def probe(self, port=None, master=None, mode=None, **overrides):
        env = {k: v for k, v in os.environ.items() if not k.startswith('EPOLL_MONITOR_TCP_')}
        if port is not None:
            env['EPOLL_MONITOR_TCP_PORT'] = str(port)
        env.update(EPOLL_MONITOR_TCP_INTERVAL_MS='100', EPOLL_MONITOR_TCP_TIMEOUT_MS='80')
        env.update(overrides)
        return subprocess.run([str(self.binary), *([mode] if mode else [])], input=json.dumps(master or self.master()),
                              env=env, text=True, capture_output=True, timeout=5)

    def exchange(self, response=PACKET, delay=0, fragment=False, mode=None):
        errors = []
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(3)
            def serve():
                try:
                    with listener.accept()[0] as conn:
                        conn.settimeout(2)
                        received = b''
                        while len(received) < 8:
                            received += conn.recv(8 - len(received))
                        self.assertEqual(received, PACKET)
                        if delay:
                            time.sleep(delay)
                        elif fragment:
                            for byte in response:
                                conn.sendall(bytes([byte])); time.sleep(.001)
                        elif response:
                            conn.sendall(response)
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=serve)
            thread.start()
            try:
                result = self.probe(listener.getsockname()[1], mode=mode)
            finally:
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_success_and_fragmented_response(self):
        result = self.exchange(fragment=True)
        self.assertEqual((result['status'], result['reason']), ('reachable', 'ping_ok'))
        self.assertGreater(result['rtt_ms'], 0)
        self.assertIsInstance(result['checked_at_unix_ms'], int)
        self.assertEqual(result['instance']['master_pid'], os.getpid())

    def test_timeout_and_closed_response(self):
        for kwargs, reason in [({'delay': .2}, 'timeout'), ({'response': b''}, 'connection_closed')]:
            with self.subTest(reason=reason):
                result = self.exchange(**kwargs)
                self.assertEqual((result['status'], result['reason']), ('unreachable', reason))
                self.assertIsNone(result['rtt_ms'])

    def test_protocol_length_code_and_crc_are_checked(self):
        for index in (1, 3, 7):
            response = bytearray(PACKET); response[index] ^= 1
            with self.subTest(index=index):
                result = self.exchange(response=bytes(response))
                self.assertEqual(result['reason'], 'protocol_error')

    def test_disabled_and_listener_closed(self):
        self.assertEqual(json.loads(self.probe().stdout)['reason'], 'disabled')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        result = json.loads(self.probe(port).stdout)
        self.assertEqual((result['status'], result['reason']), ('unreachable', 'not_listening'))

    def test_identity_mismatch_prevents_connection(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen(); listener.settimeout(.15)
            master = self.master(); master['master_start_ticks'] += 1
            result = json.loads(self.probe(listener.getsockname()[1], master=master).stdout)
            self.assertEqual(result['reason'], 'identity_unavailable')
            with self.assertRaises(socket.timeout):
                listener.accept()

    def test_stale_cache_clears_success_and_rtt(self):
        result = self.exchange(mode='stale')
        self.assertEqual((result['status'], result['reason']), ('unknown', 'stale'))
        self.assertIsNone(result['rtt_ms'])
        self.assertGreaterEqual(result['age_ms'], result['max_age_ms'])

    def test_cache_does_not_cross_instance_identity(self):
        result = self.exchange(mode='changed')
        self.assertEqual((result['status'], result['reason']), ('unknown', 'identity_changed'))
        self.assertIsNone(result['rtt_ms'])

    def test_foreign_listener_is_not_probed(self):
        child = subprocess.Popen(['python3', '-u', '-c',
            'import socket,sys;s=socket.socket();s.bind(("127.0.0.1",0));s.listen();print(s.getsockname()[1]);sys.stdin.read()'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            port = int(child.stdout.readline())
            result = json.loads(self.probe(port).stdout)
            self.assertEqual((result['status'], result['reason']), ('unknown', 'target_mismatch'))
        finally:
            child.communicate('', timeout=3)
            self.assertEqual(child.returncode, 0)

    def test_invalid_configuration_exits_before_network(self):
        for overrides in ({'EPOLL_MONITOR_TCP_PORT': '0'}, {'EPOLL_MONITOR_TCP_PORT': '65536'},
                          {'EPOLL_MONITOR_TCP_TIMEOUT_MS': '101'}, {'EPOLL_MONITOR_TCP_INTERVAL_MS': '99'},
                          {'EPOLL_MONITOR_TCP_TIMEOUT_MS': '-1'}, {'EPOLL_MONITOR_TCP_PORT': '12x'}):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.probe(**overrides).returncode, 64)


if __name__ == '__main__':
    unittest.main()
