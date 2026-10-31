#!/usr/bin/env python3
"""Prepare/import one tcp_stress_test result and matching resource summary."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import urllib.error
import urllib.parse
import urllib.request


def read_json(path):
    with path.open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("input file exceeds 8 MiB")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique), hashlib.sha256(raw).hexdigest()


def prepare_report(name, result_path, resource_path, context):
    benchmark, result_hash = read_json(result_path)
    if 'allocation' in benchmark:
        raise ValueError('balanced-load allocation/timing requires a new experiment schema; retain as standalone evidence')
    resources, resource_hash = read_json(resource_path)
    if benchmark["label"] != resources["label"]:
        raise ValueError("benchmark/resource labels do not match")
    if shlex.split(context["command"]) != resources["command"]:
        raise ValueError("resource command does not match the experiment context")
    started = datetime.fromisoformat(resources["started_utc"])
    finished = datetime.fromisoformat(benchmark["timestamp_utc"])
    duration = resources["duration_sec"]
    if (started.tzinfo is None or finished.tzinfo is None or
            not isinstance(duration, (int, float)) or isinstance(duration, bool) or
            not 0 <= duration <= 1e15 or
            not -0.001 <= (finished - started).total_seconds() <= duration + 0.001):
        raise ValueError("benchmark timestamp is outside the resource sampling window")
    report = {"name": name, "benchmark": benchmark,
              "context": {**context, "result_sha256": result_hash, "resources_sha256": resource_hash},
              "resources": {key: resources[key] for key in ("started_utc", "duration_sec", "interval_sec", "client_exit_code", "process_summary", "system_summary")}}
    if len((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()) > 65536:
        raise ValueError("prepared report exceeds 64 KiB")
    return report


def upload(url, report, token):
    target = urllib.parse.urlparse(url)
    if target.scheme != "http" or target.hostname not in ("127.0.0.1", "localhost") or target.username or target.password or target.path not in ("", "/") or target.query or target.fragment:
        raise ValueError("URL must be the local HTTP monitor origin")
    request = urllib.request.Request(url.rstrip("/") + "/api/v1/experiments",
                                     data=json.dumps(report, ensure_ascii=False).encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return json.load(response)["data"]["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True, help="sanitized reproducibility context JSON")
    parser.add_argument("--out", type=Path, help="write prepared JSON for dashboard import")
    parser.add_argument("--url", help="also import via the local HTTP API using EPOLL_MONITOR_TOKEN")
    args = parser.parse_args()
    try:
        if not args.out and not args.url:
            raise ValueError("provide --out or --url")
        context, _ = read_json(args.context)
        report = prepare_report(args.name, args.result, args.resources, context)
        if args.out:
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if args.url:
            print(upload(args.url, report, os.environ["EPOLL_MONITOR_TOKEN"]))
        return 0
    except urllib.error.HTTPError as error:
        try:
            code = json.load(error).get("code", "http_error")
        except (ValueError, OSError):
            code = "http_error"
        print(f"Import rejected: HTTP {error.code}, {code}")
    except (ValueError, KeyError, OSError) as error:
        print(f"Import failed: {type(error).__name__}; check inputs and monitor connection")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
