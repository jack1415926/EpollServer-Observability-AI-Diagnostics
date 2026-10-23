"""Offline checks for the reproducible Ping load-test client."""

import socket
import threading
import time
import unittest

from protocol import HEADER, recv_pkg, send_pkg
from tcp_stress_test import distribute_clients, percentile, run_client


class TcpStressTests(unittest.TestCase):
    def run_server(
        self, response_code, request_count, response_size=8, response_crc=0,
        fragment_response=True,
    ):
        ready = threading.Event()
        errors = []
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        def serve():
            ready.set()
            try:
                with listener, listener.accept()[0] as conn:
                    for _ in range(request_count):
                        self.assertEqual(recv_pkg(conn), (0, b""))
                        packet = HEADER.pack(response_size, response_code, response_crc)
                        if fragment_response:
                            for byte in packet:
                                conn.sendall(bytes([byte]))
                        else:
                            conn.sendall(packet)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=serve)
        thread.start()
        ready.wait(1)
        return port, thread, errors

    def test_fragmented_ping_responses_and_percentiles(self):
        for receive_mode in ("exact", "into"):
            port, thread, errors = self.run_server(0, 3)
            attempted, successful, latencies, classified = run_client(
                "127.0.0.1", port, 1, 3, receive_mode=receive_mode
            )
            thread.join(2)
            self.assertEqual(errors, [])
            self.assertEqual(attempted, 3)
            self.assertEqual(successful, 3)
            self.assertEqual(len(latencies), 3)
            self.assertEqual(classified, {})
        self.assertEqual(percentile([1, 2, 3], 95), 3)

        port, thread, errors = self.run_server(0, 1, fragment_response=False)
        attempted, successful, latencies, classified = run_client(
            "127.0.0.1", port, 1, 1, receive_mode="single", socket_mode="blocking"
        )
        thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual((attempted, successful), (1, 1))
        self.assertEqual(classified, {})

    def test_unexpected_command_is_classified(self):
        port, thread, errors = self.run_server(5, 1)
        attempted, successful, latencies, classified = run_client("127.0.0.1", port, 1, 2)
        thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(attempted, 1)
        self.assertEqual(successful, 0)
        self.assertEqual(latencies, [])
        self.assertEqual(classified, {"protocol_error": 1})

        for response_size, response_crc in ((9, 0), (8, 1)):
            port, thread, errors = self.run_server(
                0, 1, response_size=response_size, response_crc=response_crc
            )
            attempted, successful, latencies, classified = run_client(
                "127.0.0.1", port, 1, 1
            )
            thread.join(2)
            self.assertEqual(errors, [])
            self.assertEqual((attempted, successful), (1, 0))
            self.assertEqual(latencies, [])
            self.assertEqual(classified, {"protocol_error": 1})

        port, thread, errors = self.run_server(5, 1, fragment_response=False)
        attempted, successful, latencies, classified = run_client(
            "127.0.0.1", port, 1, 1, receive_mode="single",
            validation_mode="length-only",
        )
        thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual((attempted, successful), (1, 1))
        self.assertEqual(classified, {})

    def test_sampled_and_off_modes_keep_full_validation(self):
        port, thread, errors = self.run_server(0, 5)
        attempted, successful, latencies, classified = run_client(
            "127.0.0.1", port, 1, 5, latency_mode="sampled",
            latency_sample_every=2,
        )
        thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual((attempted, successful), (5, 5))
        self.assertEqual(len(latencies), 3)
        self.assertEqual(classified, {})

        port, thread, errors = self.run_server(0, 2)
        attempted, successful, latencies, classified = run_client(
            "127.0.0.1", port, 1, 2, latency_mode="off",
        )
        thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual((attempted, successful), (2, 2))
        self.assertEqual(latencies, [])
        self.assertEqual(classified, {})

    def test_process_distribution_preserves_total_concurrency(self):
        self.assertEqual(distribute_clients(10, 3), [4, 3, 3])
        self.assertEqual(sum(distribute_clients(200, 4)), 200)

    def test_kernel_timeout_is_classified(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        def serve():
            with listener, listener.accept()[0] as conn:
                self.assertEqual(recv_pkg(conn), (0, b""))
                time.sleep(0.2)

        thread = threading.Thread(target=serve)
        thread.start()
        attempted, successful, latencies, classified = run_client(
            "127.0.0.1", port, 0.05, 1, socket_mode="kernel-timeout"
        )
        thread.join(1)
        self.assertEqual((attempted, successful), (1, 0))
        self.assertEqual(latencies, [])
        self.assertEqual(classified, {"timeout": 1})


if __name__ == "__main__":
    unittest.main()
