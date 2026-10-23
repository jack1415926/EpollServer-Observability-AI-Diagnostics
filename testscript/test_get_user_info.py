#!/usr/bin/env python3
"""Query known existing/missing IDs twice; responses alone do not prove cache hits."""
import socket
import struct
import sys
from protocol import check_timeout, connection_args, recv_pkg, require, send_pkg

def query(sock, user_id, expected):
    send_pkg(sock, 7, struct.pack("!q", user_id))
    code, body = recv_pkg(sock)
    require(code == 7, f"unexpected command {code}")
    require(len(body) == 68, "expected 68-byte user response")
    result, returned_id, raw_name = struct.unpack("!iq56s", body)
    require(result == expected, f"user {user_id}: expected result {expected}, got {result}")
    require(returned_id == user_id, f"user ID mismatch: {returned_id}")
    name = raw_name.split(b"\0", 1)[0].decode("utf-8")
    require(bool(name) == (expected == 0), "unexpected username for result")
    return result, returned_id, name

def main():
    parser = connection_args(__doc__)
    parser.add_argument("--exist-id", type=int, default=1)
    parser.add_argument("--missing-id", type=int, default=99999)
    args = parser.parse_args()
    check_timeout(args.timeout)
    require(0 < args.exist_id < 2**63 and 0 < args.missing_id < 2**63,
            "IDs must be positive signed 64-bit integers")
    require(args.exist_id != args.missing_id, "existing and missing IDs must differ")
    with socket.create_connection((args.host, args.port), args.timeout) as sock:
        for user_id, expected in ((args.exist_id, 0), (args.missing_id, 1)):
            first = query(sock, user_id, expected)
            second = query(sock, user_id, expected)
            require(first == second, f"user {user_id}: repeated responses differ")
            print(f"PASS: user {user_id}, result={expected}, repeated response consistent")
    print("PASS: user queries (cache hits require separate Redis observation)")

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
