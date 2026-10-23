#!/usr/bin/env python3
"""Inject query preparation failure into isolated Workers; no accounts or DB data are changed."""
import argparse
import os
from pathlib import Path
import secrets
import re
import signal
import socket
import struct
import subprocess
import tempfile

from protocol import recv_pkg, require, send_pkg
from test_shutdown import children, configure, connect
from test_worker_snapshot import wait_for


def cache_exists(user_id):
    key = f"user:info:{user_id}".encode()
    with socket.create_connection(("127.0.0.1", 6379), timeout=5) as redis:
        redis.sendall(b"*2\r\n$6\r\nEXISTS\r\n$" + str(len(key)).encode() + b"\r\n" + key + b"\r\n")
        with redis.makefile("rb") as stream:
            reply = stream.readline(128)
    require(reply in (b":0\r\n", b":1\r\n"), "unexpected Redis EXISTS response")
    return reply == b":1\r\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    source = binary.with_name("nginx.conf").read_text()
    root = Path(tempfile.mkdtemp(prefix="epoll-backend-failure-"))
    print(f"Test logs: {root}", flush=True)
    library_source = root / "fault.c"
    library_source.write_text("""
#include <stddef.h>
/* This process runs only Ping/GetUserInfo. Startup and connection checks remain real. */
int mysql_stmt_prepare(void *statement, const char *query, unsigned long length) {
    (void)statement; (void)query; (void)length;
    return 1;
}
""")
    library = root / "fault.so"
    subprocess.run(["gcc", "-shared", "-fPIC", str(library_source), "-o", str(library)], check=True)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    config = root / "nginx.conf"
    isolated = "\n".join(line for line in configure(source, port).splitlines()
                         if not re.match(r"\s*WorkerSnapshotEnable\s*=", line, re.IGNORECASE))
    config.write_text(isolated + "\nWorkerSnapshotEnable = 0\n")
    config.chmod(0o600)
    proc = None
    workers = []
    try:
        with (root / "console.log").open("wb") as output:
            proc = subprocess.Popen([str(binary)], cwd=root, stdout=output, stderr=subprocess.STDOUT,
                                    start_new_session=True, env={**os.environ, "LD_PRELOAD": str(library)})
            log = root / "server.log"
            def ready():
                require(proc.poll() is None, "isolated server exited during startup")
                return log.exists() and log.read_text(errors="replace").count("【worker进程】启动") == 2
            wait_for(ready, timeout=30)
            workers = children(proc.pid)
            user_id = secrets.randbelow(10**12) + 10**12
            require(not cache_exists(user_id), "random test key already exists")
            with connect(port, "127.0.11.1") as client:
                for _ in range(2):
                    send_pkg(client, 7, struct.pack("!q", user_id))
                    code, body = recv_pkg(client)
                    require(code == 7 and len(body) == 68, "invalid GetUserInfo response")
                    require(struct.unpack("!i", body[:4])[0] == 3, "query error was reported as user absence")
                    require(not cache_exists(user_id), "query error polluted the negative cache")
                send_pkg(client, 0, b"")
                require(recv_pkg(client) == (0, b""), "query failure interrupted Ping")
            proc.send_signal(signal.SIGTERM)
            require(proc.wait(timeout=30) == 0, "isolated server did not stop cleanly")
            require(all(not Path(f"/proc/{pid}").exists() for pid in workers), "Worker remains")
            print("PASS: query errors return backend failure, never write NULL_USER, and Ping remains available")
    finally:
        if proc is not None:
            if proc.poll() is None or any(Path(f"/proc/{pid}").exists() for pid in workers):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait(timeout=5)
        config.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
