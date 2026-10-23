"""Local stdio MCP server exposing read-only EpollServer diagnostics."""

from __future__ import annotations

from collections import deque
from pathlib import Path
import re
import socket
import sys
import time
from typing import Annotated, TypedDict

from mcp.server.fastmcp import FastMCP
from pydantic import Field, StringConstraints

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from testscript.protocol import recv_pkg, send_pkg


LOG_PATH = PROJECT_ROOT / "server" / "error.log"
LOG_DISPLAY_PATH = "server/error.log"
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|api[_ -]?key|secret|token)\b(\s*[:=]\s*)(\S+)"
)

Host = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=253),
]
Query = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
LogFilter = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]


class PingResult(TypedDict):
    ok: bool
    host: str
    port: int
    latency_ms: float
    error_code: str | None
    message: str


class SearchMatch(TypedDict):
    path: str
    line: int
    text: str


class SearchResult(TypedDict):
    query: str
    total: int
    count: int
    truncated: bool
    matches: list[SearchMatch]


class LogResult(TypedDict):
    status: str
    path: str
    total_matching: int
    count: int
    lines: list[str]
    message: str


mcp = FastMCP(
    "epoll_server_mcp",
    instructions="Read-only health, documentation, and log tools for Learn_EpollServer.",
)


def _ping_result(
    ok: bool,
    host: str,
    port: int,
    started: float,
    error_code: str | None,
    message: str,
) -> PingResult:
    return {
        "ok": ok,
        "host": host,
        "port": port,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "error_code": error_code,
        "message": message,
    }


@mcp.tool(
    name="epoll_ping_server",
    title="Ping EpollServer",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    structured_output=True,
)
def epoll_ping_server(
    host: Host = "127.0.0.1",
    port: Annotated[int, Field(ge=1, le=65535)] = 8080,
    timeout: Annotated[float, Field(ge=0.1, le=10.0)] = 2.0,
) -> PingResult:
    """Send the existing binary Ping command and validate its complete response."""
    started = time.perf_counter()
    try:
        sock = socket.create_connection((host, port), timeout)
    except TimeoutError:
        return _ping_result(False, host, port, started, "timeout", "Connection timed out.")
    except OSError:
        return _ping_result(
            False,
            host,
            port,
            started,
            "connection_failed",
            "Could not connect; check that EpollServer is running and reachable.",
        )

    with sock:
        sock.settimeout(timeout)
        try:
            send_pkg(sock, 0, b"")
            code, body = recv_pkg(sock)
        except TimeoutError:
            return _ping_result(False, host, port, started, "timeout", "Ping response timed out.")
        except (ConnectionError, ValueError):
            return _ping_result(
                False,
                host,
                port,
                started,
                "invalid_response",
                "Server returned an incomplete or CRC-invalid response.",
            )
        except OSError:
            return _ping_result(False, host, port, started, "network_error", "Ping network I/O failed.")

    if code != 0 or body:
        return _ping_result(
            False,
            host,
            port,
            started,
            "unexpected_response",
            f"Expected command 0 with an empty body; received command {code} and {len(body)} body bytes.",
        )
    return _ping_result(True, host, port, started, None, "EpollServer Ping succeeded.")


def _document_paths() -> list[Path]:
    fixed = [
        PROJECT_ROOT / "readme.md",
        PROJECT_ROOT / "CLAUDE.md",
        PROJECT_ROOT / "sql" / "README.md",
        PROJECT_ROOT / "qt-client" / "README.md",
    ]
    return sorted(
        (path for path in [*fixed, *PROJECT_ROOT.glob("docs/**/*.md")] if path.is_file()),
        key=lambda path: path.relative_to(PROJECT_ROOT).as_posix().casefold(),
    )


def _redact_doc_line(line: str) -> str:
    return SENSITIVE_ASSIGNMENT.sub(r"\1\2[REDACTED]", line)


@mcp.tool(
    name="epoll_search_docs",
    title="Search EpollServer Documentation",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    structured_output=True,
)
def epoll_search_docs(
    query: Query,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> SearchResult:
    """Search an allowlist of project Markdown files using a literal query."""
    needle = query.casefold()
    matches: list[SearchMatch] = []
    total = 0
    for path in _document_paths():
        try:
            document_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(document_lines, 1):
            if needle not in line.casefold():
                continue
            total += 1
            if len(matches) < limit:
                matches.append(
                    {
                        "path": path.relative_to(PROJECT_ROOT).as_posix(),
                        "line": line_number,
                        "text": _redact_doc_line(line.strip()),
                    }
                )
    return {
        "query": query,
        "total": total,
        "count": len(matches),
        "truncated": total > len(matches),
        "matches": matches,
    }


@mcp.tool(
    name="epoll_tail_log",
    title="Read EpollServer Log Tail",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    structured_output=True,
)
def epoll_tail_log(
    lines: Annotated[int, Field(ge=1, le=500)] = 100,
    contains: LogFilter | None = None,
) -> LogResult:
    """Read the tail of the fixed server/error.log file with optional literal filtering."""
    if not LOG_PATH.exists():
        return {
            "status": "missing",
            "path": LOG_DISPLAY_PATH,
            "total_matching": 0,
            "count": 0,
            "lines": [],
            "message": "Log file does not exist; start EpollServer or check its Log setting.",
        }

    needle = contains.casefold() if contains else None
    tail: deque[str] = deque(maxlen=lines)
    total = 0
    try:
        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as log_file:
            for raw_line in log_file:
                line = raw_line.rstrip("\r\n")
                if needle is not None and needle not in line.casefold():
                    continue
                total += 1
                tail.append(line)
    except OSError:
        return {
            "status": "unreadable",
            "path": LOG_DISPLAY_PATH,
            "total_matching": 0,
            "count": 0,
            "lines": [],
            "message": "Log file could not be read; check local file permissions.",
        }

    result_lines = list(tail)
    return {
        "status": "ok" if result_lines else "no_matches",
        "path": LOG_DISPLAY_PATH,
        "total_matching": total,
        "count": len(result_lines),
        "lines": result_lines,
        "message": "Log tail returned." if result_lines else "No log lines matched the filter.",
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
