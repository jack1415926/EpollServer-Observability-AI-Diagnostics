"""Shared binary protocol helpers for integration scripts."""
import argparse
import math
import struct
import zlib

HEADER = struct.Struct("!HHI")

def connection_args(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser

def check_timeout(value):
    require(math.isfinite(value) and value > 0, "timeout must be finite and positive")

def require(condition, message):
    # Remains enabled under python -O.
    if not condition:
        raise ValueError(message)

def send_pkg(sock, msg_code, body):
    sock.sendall(HEADER.pack(8 + len(body), msg_code, zlib.crc32(body) & 0xffffffff) + body)

def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError(f"EOF: expected {size} bytes, received {len(data)}")
        data.extend(chunk)
    return bytes(data)

def recv_pkg(sock):
    size, code, crc = HEADER.unpack(recv_exact(sock, 8))
    require(8 <= size <= 30000, f"invalid packet length: {size}")
    body = recv_exact(sock, size - 8)
    require(zlib.crc32(body) & 0xffffffff == crc, "response CRC mismatch")
    return code, body
