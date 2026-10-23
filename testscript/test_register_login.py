#!/usr/bin/env python3
"""Register/login with a unique account; each run creates one database row."""
import socket
import struct
import sys
import uuid
from protocol import check_timeout, connection_args, recv_pkg, require, send_pkg

def main():
    args = connection_args(__doc__).parse_args()
    check_timeout(args.timeout)
    user = ("test_" + uuid.uuid4().hex).encode()
    body = struct.pack("!i56s40s", 0, user, b"pass1234")
    with socket.create_connection((args.host, args.port), args.timeout) as sock:
        for command, label in ((5, "register"), (6, "login")):
            send_pkg(sock, command, body)
            code, response = recv_pkg(sock)
            require(code == command, f"{label}: unexpected command {code}")
            require(len(response) == 100, f"{label}: expected 100-byte body")
            result, username, _ = struct.unpack("!i56s40s", response)
            require(result == 0, f"{label}: business error {result}")
            require(username.split(b"\0", 1)[0] == user, f"{label}: username mismatch")
            print(f"PASS: {label}")
    print(f"PASS: register/login; created user {user.decode()}")

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
