"""Offline checks for the read-only EpollServer MCP tools."""

import asyncio
from contextlib import contextmanager
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock
import zlib

from epoll_mcp.server import (
    epoll_ping_server,
    epoll_search_docs,
    epoll_tail_log,
    mcp,
)


@contextmanager
def response_server(packet: bytes | None, delay: float = 0.0):
    errors = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)
        port = listener.getsockname()[1]

        def serve():
            try:
                with listener.accept()[0] as conn:
                    conn.settimeout(2)
                    conn.recv(8)
                    if delay:
                        time.sleep(delay)
                    if packet is not None:
                        for byte in packet:
                            conn.sendall(bytes([byte]))
            except Exception as exc:  # surfaced by the owning test
                errors.append(exc)

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            yield port
        finally:
            thread.join(3)
            if thread.is_alive():
                errors.append(TimeoutError("test response server did not stop"))
            if errors:
                raise errors[0]


def packet(code: int = 0, body: bytes = b"", corrupt_crc: bool = False) -> bytes:
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return struct.pack("!HHI", 8 + len(body), code, crc ^ int(corrupt_crc)) + body


class McpToolTests(unittest.TestCase):
    def test_tool_registration_and_read_only_annotations(self):
        tools = asyncio.run(mcp.list_tools())
        self.assertEqual(
            {tool.name for tool in tools},
            {"epoll_ping_server", "epoll_search_docs", "epoll_tail_log"},
        )
        for tool in tools:
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)
            self.assertTrue(tool.annotations.idempotentHint)
        schemas = {tool.name: tool.inputSchema for tool in tools}
        self.assertNotIn("path", schemas["epoll_search_docs"]["properties"])
        self.assertNotIn("path", schemas["epoll_tail_log"]["properties"])
        self.assertEqual(schemas["epoll_ping_server"]["properties"]["port"]["maximum"], 65535)

    def test_ping_success_and_failures(self):
        with response_server(packet()) as port:
            result = epoll_ping_server(port=port)
        self.assertTrue(result["ok"])

        for response, expected in (
            (packet(corrupt_crc=True), "invalid_response"),
            (packet(code=5), "unexpected_response"),
        ):
            with self.subTest(expected=expected), response_server(response) as port:
                result = epoll_ping_server(port=port)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], expected)

        with mock.patch(
            "epoll_mcp.server.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ):
            result = epoll_ping_server(port=1, timeout=0.1)
        self.assertEqual(result["error_code"], "connection_failed")

        with response_server(None, delay=0.3) as port:
            result = epoll_ping_server(port=port, timeout=0.1)
        self.assertEqual(result["error_code"], "timeout")

    def test_document_search_is_literal_limited_and_located(self):
        result = epoll_search_docs("EpollServer", limit=2)
        self.assertEqual(result["count"], 2)
        self.assertGreaterEqual(result["total"], result["count"])
        self.assertTrue(result["truncated"])
        for match in result["matches"]:
            self.assertGreater(match["line"], 0)
            self.assertFalse(Path(match["path"]).is_absolute())

        self.assertEqual(epoll_search_docs("../../.git", limit=20)["matches"], [])

    def test_log_tail_filter_missing_and_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "error.log"
            log_path.write_bytes("one\nRedis ready\nthree\nRedis down\n".encode() + b"bad:\xff\n")
            with mock.patch("epoll_mcp.server.LOG_PATH", log_path):
                result = epoll_tail_log(lines=1, contains="redis")
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["total_matching"], 2)
                self.assertEqual(result["lines"], ["Redis down"])
                self.assertIn("\ufffd", epoll_tail_log(lines=1)["lines"][0])

            with mock.patch("epoll_mcp.server.LOG_PATH", log_path.with_name("missing.log")):
                result = epoll_tail_log()
                self.assertEqual(result["status"], "missing")
                self.assertEqual(result["lines"], [])


if __name__ == "__main__":
    unittest.main()
