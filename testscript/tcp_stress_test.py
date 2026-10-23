"""Reproducible Ping load test for the current EpollServer binary protocol."""

import collections
import concurrent.futures
import errno
import json
import math
import os
import platform
import socket
import subprocess
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from protocol import HEADER, connection_args, recv_exact, require


PING_PACKET = HEADER.pack(HEADER.size, 0, 0)


def parse_args():
    parser = connection_args(__doc__)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests-per-client", type=int, default=10)
    parser.add_argument("--processes", type=int, default=1)
    parser.add_argument(
        "--receive-mode", choices=("exact", "into", "single"), default="into",
        help="single is diagnostic only and rejects fragmented responses",
    )
    parser.add_argument(
        "--validation-mode", choices=("strict", "length-only"), default="strict",
        help="length-only is diagnostic and must not be used for authoritative results",
    )
    parser.add_argument(
        "--socket-mode", choices=("timeout", "kernel-timeout", "blocking"),
        default="kernel-timeout",
        help="blocking keeps the connect timeout but has no response timeout",
    )
    parser.add_argument(
        "--latency-mode", choices=("all", "sampled", "off"), default="all",
    )
    parser.add_argument("--latency-sample-every", type=int, default=100)
    parser.add_argument("--source-ip", help="optional local address to bind before connecting")
    parser.add_argument("--label", default="unspecified")
    parser.add_argument("--build-mode", choices=("debug", "release", "unknown"), default="unknown")
    parser.add_argument("--server-workers", type=int)
    parser.add_argument("--worker-threads", type=int)
    parser.add_argument(
        "--rate-limit-mode", choices=("enabled", "disabled", "unknown"), default="unknown",
    )
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    require(args.concurrency > 0, "concurrency must be positive")
    require(args.requests_per_client > 0, "requests-per-client must be positive")
    require(args.concurrency <= 2000, "concurrency must not exceed 2000")
    require(0 < args.processes <= args.concurrency, "processes must be between 1 and concurrency")
    require(args.latency_sample_every > 0, "latency-sample-every must be positive")
    require(args.server_workers is None or args.server_workers > 0, "server-workers must be positive")
    require(args.worker_threads is None or args.worker_threads > 0, "worker-threads must be positive")
    require(math.isfinite(args.timeout) and args.timeout > 0, "timeout must be finite and positive")
    return args


def classify_error(exc):
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, BlockingIOError) and exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
        return "timeout"
    if isinstance(exc, ConnectionResetError):
        return "connection_reset"
    if isinstance(exc, BrokenPipeError):
        return "broken_pipe"
    if isinstance(exc, ConnectionError):
        return "eof"
    if isinstance(exc, ValueError):
        return "protocol_error"
    if isinstance(exc, OSError):
        return "network_error"
    return "unexpected_error"


def configure_connected_socket(sock, timeout, socket_mode):
    if socket_mode == "timeout":
        return
    sock.settimeout(None)
    if socket_mode == "kernel-timeout":
        seconds = int(timeout)
        microseconds = int((timeout - seconds) * 1_000_000)
        timeval = struct.pack("@ll", seconds, microseconds)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, timeval)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, timeval)


def exchange_ping(sock, receive_mode, receive_buffer, validation_mode):
    sock.sendall(PING_PACKET)
    if receive_mode == "exact":
        response = recv_exact(sock, HEADER.size)
    elif receive_mode == "into":
        view = memoryview(receive_buffer)
        received = 0
        while received < HEADER.size:
            count = sock.recv_into(view[received:])
            if count == 0:
                raise ConnectionError(
                    f"EOF: expected {HEADER.size} bytes, received {received}"
                )
            received += count
        response = receive_buffer
    else:
        response = sock.recv(HEADER.size)
        require(
            len(response) == HEADER.size,
            f"fragmented Ping response: expected {HEADER.size} bytes, received {len(response)}",
        )
    if validation_mode == "length-only":
        return
    size, code, crc = HEADER.unpack(response)
    if size != HEADER.size or code != 0 or crc != 0:
        raise ValueError(
            f"invalid Ping response: length={size}, command={code}, crc={crc}"
        )


def run_client(
    host, port, timeout, request_count, source_ip=None,
    latency_mode="all", latency_sample_every=100, receive_mode="into",
    validation_mode="strict", socket_mode="kernel-timeout",
):
    latencies_ms = []
    errors = collections.Counter()
    attempted = 0
    successful = 0
    receive_buffer = bytearray(HEADER.size) if receive_mode == "into" else None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if source_ip:
                sock.bind((source_ip, 0))
            sock.connect((host, port))
            configure_connected_socket(sock, timeout, socket_mode)

            if (
                latency_mode == "off"
                and receive_mode == "single"
                and validation_mode == "length-only"
            ):
                try:
                    for _ in range(request_count):
                        attempted += 1
                        sock.sendall(PING_PACKET)
                        response = sock.recv(HEADER.size)
                        if len(response) != HEADER.size:
                            raise ValueError(
                                f"fragmented Ping response: expected {HEADER.size} bytes, "
                                f"received {len(response)}"
                            )
                        successful += 1
                except Exception as exc:
                    errors[classify_error(exc)] += 1
                return attempted, successful, latencies_ms, errors

            for request_index in range(request_count):
                attempted += 1
                measure_latency = (
                    latency_mode == "all"
                    or latency_mode == "sampled"
                    and request_index % latency_sample_every == 0
                )
                started = time.perf_counter() if measure_latency else None
                try:
                    exchange_ping(sock, receive_mode, receive_buffer, validation_mode)
                    successful += 1
                    if measure_latency:
                        latencies_ms.append((time.perf_counter() - started) * 1000)
                except Exception as exc:
                    errors[classify_error(exc)] += 1
                    break
    except Exception as exc:
        attempted = 1
        errors[classify_error(exc)] += 1
    return attempted, successful, latencies_ms, errors


def run_thread_group(
    host, port, timeout, concurrency, request_count, source_ip,
    latency_mode, latency_sample_every, receive_mode, validation_mode, socket_mode,
):
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(
                run_client, host, port, timeout, request_count, source_ip,
                latency_mode, latency_sample_every, receive_mode, validation_mode,
                socket_mode,
            )
            for _ in range(concurrency)
        ]

    attempted = 0
    successful = 0
    latencies_ms = []
    errors = collections.Counter()
    for future in futures:
        result = future.result()
        attempted += result[0]
        successful += result[1]
        latencies_ms.extend(result[2])
        errors.update(result[3])
    return attempted, successful, latencies_ms, errors


def distribute_clients(concurrency, processes):
    quotient, remainder = divmod(concurrency, processes)
    return [quotient + (index < remainder) for index in range(processes)]


def run_thread_group_from_tuple(arguments):
    return run_thread_group(*arguments)


def percentile(sorted_values, percent):
    if not sorted_values:
        return None
    index = max(0, math.ceil(percent / 100 * len(sorted_values)) - 1)
    return round(sorted_values[index], 3)


def git_commit():
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def git_dirty():
    try:
        return bool(subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=True, timeout=2,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None


def memory_total_mib():
    try:
        first_line = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()[0]
        return round(int(first_line.split()[1]) / 1024, 1)
    except (OSError, IndexError, ValueError):
        return None


def main():
    try:
        args = parse_args()
        requested = args.concurrency * args.requests_per_client
        print(f"Target: {args.host}:{args.port}")
        print(f"Load: {args.concurrency} clients x {args.requests_per_client} requests = {requested}")
        print(f"Label: {args.label}")

        started = time.perf_counter()
        group_args = [
            (
                args.host, args.port, args.timeout, group_concurrency,
                args.requests_per_client, args.source_ip, args.latency_mode,
                args.latency_sample_every, args.receive_mode, args.validation_mode,
                args.socket_mode,
            )
            for group_concurrency in distribute_clients(args.concurrency, args.processes)
        ]
        if args.processes == 1:
            group_results = [run_thread_group(*group_args[0])]
        else:
            with concurrent.futures.ProcessPoolExecutor(max_workers=args.processes) as pool:
                group_results = list(pool.map(run_thread_group_from_tuple, group_args))
        duration = time.perf_counter() - started

        attempted = 0
        successful = 0
        latencies_ms = []
        errors = collections.Counter()
        for client_attempted, client_successful, client_latencies, client_errors in group_results:
            attempted += client_attempted
            successful += client_successful
            latencies_ms.extend(client_latencies)
            errors.update(client_errors)

        failed = sum(errors.values())
        not_attempted = requested - attempted
        ordered = sorted(latencies_ms)
        result = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "label": args.label,
            "git_commit": git_commit(),
            "git_dirty": git_dirty(),
            "system": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "memory_total_mib": memory_total_mib(),
            "target": {"host": args.host, "port": args.port},
            "server": {
                "build_mode": args.build_mode,
                "workers": args.server_workers,
                "worker_threads": args.worker_threads,
                "rate_limit_mode": args.rate_limit_mode,
            },
            "load": {
                "concurrency": args.concurrency,
                "requests_per_client": args.requests_per_client,
                "requested": requested,
                "processes": args.processes,
                "receive_mode": args.receive_mode,
                "validation_mode": args.validation_mode,
                "socket_mode": args.socket_mode,
                "latency_mode": args.latency_mode,
                "latency_sample_every": (
                    args.latency_sample_every if args.latency_mode == "sampled" else None
                ),
            },
            "result": {
                "duration_sec": round(duration, 3),
                "attempted": attempted,
                "successful": successful,
                "failed": failed,
                "not_attempted": not_attempted,
                "success_rate_percent": round(successful / requested * 100, 3),
                "qps": round(successful / duration, 3) if duration else 0,
                "latency_ms": {
                    "sample_count": len(ordered),
                    "p50": percentile(ordered, 50),
                    "p95": percentile(ordered, 95),
                    "p99": percentile(ordered, 99),
                    "max": round(ordered[-1], 3) if ordered else None,
                },
                "errors": dict(sorted(errors.items())),
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return 0 if successful == requested else 1
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
