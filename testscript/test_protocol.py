"""Offline regression checks: python -m unittest discover -s testscript -p test_protocol.py"""
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import zlib
from pathlib import Path

from protocol import recv_pkg, send_pkg


class FragmentSocket:
    def __init__(self, data):
        self.data = data

    def recv(self, size):
        chunk, self.data = self.data[:min(size, 1)], self.data[min(size, 1):]
        return chunk


class ProtocolTests(unittest.TestCase):
    def test_known_crc_and_fragmented_coalesced_packets(self):
        # Standard CRC32 vector, high bit set: must be packed as unsigned.
        packet = struct.pack("!HHI", 17, 5, 0xcbf43926) + b"123456789"
        class Capture:
            def sendall(self, data):
                self.data = data
        capture = Capture()
        send_pkg(capture, 5, b"123456789")
        self.assertEqual(capture.data, packet)
        sock = FragmentSocket(packet + struct.pack("!HHI", 8, 0, 0))
        self.assertEqual(recv_pkg(sock), (5, b"123456789"))
        self.assertEqual(recv_pkg(sock), (0, b""))

    def test_invalid_packets(self):
        for packet, error in (
            (b"\0", ConnectionError),
            (struct.pack("!HHI", 10, 5, 0) + b"x", ConnectionError),
            (struct.pack("!HHI", 7, 5, 0), ValueError),
            (struct.pack("!HHI", 30001, 5, 0), ValueError),
            (struct.pack("!HHI", 9, 5, 0) + b"x", ValueError),
        ):
            with self.subTest(packet=packet), self.assertRaises(error):
                recv_pkg(FragmentSocket(packet))

    def run_script(self, script, fault=None):
        errors = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)

            def serve():
                try:
                    with listener.accept()[0] as conn:
                        conn.settimeout(3)
                        for _ in range(2 if script == "test_register_login.py" else 4):
                            code, body = recv_pkg(conn)
                            if fault == "timeout":
                                time.sleep(0.4)
                                return
                            if fault == "eof":
                                return
                            if code == 7:
                                user_id, = struct.unpack("!q", body)
                                response = struct.pack("!iq56s", 0 if user_id == 1 else 1,
                                                       user_id, b"alice" if user_id == 1 else b"")
                            else:
                                response = body[:60] + bytes(40)
                            if fault == "business":
                                response = struct.pack("!i", 3) + response[4:]
                            if fault == "length":
                                response = response[:-1]
                            if fault == "identity":
                                offset = 12 if code == 7 else 4
                                response = response[:offset] + b"!" + response[offset + 1:]
                                if code == 7:
                                    response = response[:4] + struct.pack("!q", 42) + response[12:]
                            crc = zlib.crc32(response) & 0xffffffff
                            packet = struct.pack("!HHI", 8 + len(response),
                                                 99 if fault == "command" else code,
                                                 crc ^ (1 if fault == "crc" else 0)) + response
                            conn.sendall(packet[:3])
                            conn.sendall(packet[3:])
                            if fault:
                                return
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=serve)
            thread.start()
            result = subprocess.run(
                [sys.executable, "-O", str(Path(__file__).with_name(script)),
                 "--port", str(listener.getsockname()[1]), "--timeout", "0.2"],
                capture_output=True, text=True, timeout=5)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result.returncode, 1 if fault else 0, result.stderr)
            self.assertIn("FAIL:" if fault else "PASS:", result.stderr if fault else result.stdout)

    def test_script_exit_codes(self):
        for script in ("test_register_login.py", "test_get_user_info.py"):
            for fault in (None, "crc", "command", "length", "business", "identity", "eof", "timeout"):
                with self.subTest(script=script, fault=fault):
                    self.run_script(script, fault)


if __name__ == "__main__":
    unittest.main()
