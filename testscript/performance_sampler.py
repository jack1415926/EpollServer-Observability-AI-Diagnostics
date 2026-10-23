"""Run a load-test command while sampling EpollServer and Redis resources."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


QUEUE_RE = re.compile(
    r"当前收消息队列/发消息队列大小分别为\((\d+)/(\d+)\)，"
    r"丢弃的待发送数据包数量为(\d+)"
)
PID_RE = re.compile(r"\]\s+(\d+):")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-pid", type=int, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, default=Path("error.log"))
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument(
        "command", nargs=argparse.REMAINDER,
        help="load-test command, preceded by --",
    )
    args = parser.parse_args()
    if args.master_pid <= 0:
        parser.error("--master-pid must be positive")
    if not (0.05 <= args.interval <= 60):
        parser.error("--interval must be between 0.05 and 60 seconds")
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a load-test command is required after --")
    return args


def parse_stat(text):
    close_paren = text.rfind(")")
    if close_paren < 0:
        raise ValueError("invalid /proc stat line")
    fields = text[close_paren + 2:].split()
    if len(fields) < 13:
        raise ValueError("incomplete /proc stat line")
    return {
        "ppid": int(fields[1]),
        "cpu_ticks": int(fields[11]) + int(fields[12]),
        "processor": int(fields[36]) if len(fields) > 36 else None,
    }


def parse_status(text):
    values = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key] = value.strip()
    return {
        "rss_kib": int(values.get("VmRSS", "0 kB").split()[0]),
        "threads": int(values.get("Threads", "0")),
        "voluntary_context_switches": int(values.get("voluntary_ctxt_switches", "0")),
        "nonvoluntary_context_switches": int(
            values.get("nonvoluntary_ctxt_switches", "0")
        ),
    }


def read_process(pid, proc_root=Path("/proc")):
    process_dir = proc_root / str(pid)
    stat = parse_stat((process_dir / "stat").read_text(encoding="ascii"))
    status = parse_status((process_dir / "status").read_text(encoding="ascii"))
    name = (process_dir / "comm").read_text(encoding="utf-8").strip()
    return {"pid": pid, "name": name, **stat, **status}


def child_pids(pid, proc_root=Path("/proc")):
    path = proc_root / str(pid) / "task" / str(pid) / "children"
    try:
        return [int(value) for value in path.read_text(encoding="ascii").split()]
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return []


def descendant_pids(pid, proc_root=Path("/proc")):
    descendants = []
    pending = child_pids(pid, proc_root)
    while pending:
        child = pending.pop(0)
        descendants.append(child)
        pending.extend(child_pids(child, proc_root))
    return descendants


def find_redis_pids(proc_root=Path("/proc")):
    found = []
    for path in proc_root.iterdir():
        if not path.name.isdigit():
            continue
        try:
            name = (path / "comm").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if name.startswith("redis-server"):
            found.append(int(path.name))
    return sorted(found)


def sample_processes(targets, previous, elapsed, clock_ticks, proc_root=Path("/proc")):
    samples = []
    current = {}
    for role, pid in targets:
        try:
            process = read_process(pid, proc_root)
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            continue
        old_ticks = previous.get(pid)
        cpu_percent = None
        if old_ticks is not None and elapsed > 0:
            cpu_percent = round(
                (process["cpu_ticks"] - old_ticks) / clock_ticks / elapsed * 100, 2
            )
        current[pid] = process["cpu_ticks"]
        samples.append({
            "role": role,
            "pid": pid,
            "name": process["name"],
            "cpu_percent": cpu_percent,
            "rss_kib": process["rss_kib"],
            "threads": process["threads"],
            "processor": process["processor"],
            "voluntary_context_switches": process["voluntary_context_switches"],
            "nonvoluntary_context_switches": process["nonvoluntary_context_switches"],
        })
    return samples, current


def read_system(proc_root=Path("/proc")):
    stat_lines = (proc_root / "stat").read_text(encoding="ascii").splitlines()
    cpu = [int(value) for value in stat_lines[0].split()[1:9]]
    counters = {}
    for line in stat_lines[1:]:
        key, separator, value = line.partition(" ")
        if key in {"ctxt", "processes"} and separator:
            counters[key] = int(value.strip())
    load_values = (proc_root / "loadavg").read_text(encoding="ascii").split()
    return {
        "cpu_ticks": cpu,
        "context_switches": counters.get("ctxt", 0),
        "processes_created": counters.get("processes", 0),
        "load_1m": float(load_values[0]),
        "load_5m": float(load_values[1]),
        "load_15m": float(load_values[2]),
    }


def system_delta(current, previous, elapsed):
    result = {
        "cpu_busy_percent": None,
        "cpu_user_percent": None,
        "cpu_system_percent": None,
        "cpu_idle_percent": None,
        "cpu_iowait_percent": None,
        "cpu_steal_percent": None,
        "context_switches_per_sec": None,
        "processes_created_per_sec": None,
        "load_1m": current["load_1m"],
        "load_5m": current["load_5m"],
        "load_15m": current["load_15m"],
    }
    if previous is None or elapsed <= 0:
        return result

    deltas = [
        max(0, value - old)
        for value, old in zip(current["cpu_ticks"], previous["cpu_ticks"])
    ]
    total = sum(deltas)
    if total:
        user, nice, system, idle, iowait, irq, softirq, steal = deltas
        percent = lambda value: round(value / total * 100, 2)
        result.update({
            "cpu_busy_percent": percent(total - idle - iowait),
            "cpu_user_percent": percent(user + nice),
            "cpu_system_percent": percent(system + irq + softirq),
            "cpu_idle_percent": percent(idle),
            "cpu_iowait_percent": percent(iowait),
            "cpu_steal_percent": percent(steal),
        })
    result["context_switches_per_sec"] = round(
        (current["context_switches"] - previous["context_switches"]) / elapsed, 2
    )
    result["processes_created_per_sec"] = round(
        (current["processes_created"] - previous["processes_created"]) / elapsed, 2
    )
    return result


def read_queue_events(path, offset):
    try:
        size = path.stat().st_size
        if size < offset:
            offset = 0
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            lines = stream.readlines()
            new_offset = stream.tell()
    except FileNotFoundError:
        return [], offset

    events = []
    for line in lines:
        match = QUEUE_RE.search(line)
        if not match:
            continue
        pid_match = PID_RE.search(line)
        events.append({
            "pid": int(pid_match.group(1)) if pid_match else None,
            "recv_queue": int(match.group(1)),
            "send_queue": int(match.group(2)),
            "discarded_send_packets": int(match.group(3)),
            "line": line.rstrip("\n"),
        })
    return events, new_offset


def process_summary(samples):
    grouped = {}
    for sample in samples:
        for process in sample["processes"]:
            key = f'{process["role"]}:{process["pid"]}'
            item = grouped.setdefault(key, {
                "role": process["role"], "pid": process["pid"],
                "name": process["name"], "cpu_values": [],
                "max_rss_kib": 0, "max_threads": 0,
            })
            if process["cpu_percent"] is not None:
                item["cpu_values"].append(process["cpu_percent"])
            item["max_rss_kib"] = max(item["max_rss_kib"], process["rss_kib"])
            item["max_threads"] = max(item["max_threads"], process["threads"])

    result = []
    for item in grouped.values():
        cpu_values = item.pop("cpu_values")
        item["avg_cpu_percent"] = (
            round(sum(cpu_values) / len(cpu_values), 2) if cpu_values else None
        )
        item["max_cpu_percent"] = max(cpu_values) if cpu_values else None
        result.append(item)
    return sorted(result, key=lambda item: (item["role"], item["pid"]))


def system_summary(samples):
    fields = (
        "cpu_busy_percent", "cpu_user_percent", "cpu_system_percent",
        "cpu_iowait_percent", "cpu_steal_percent", "context_switches_per_sec",
    )
    result = {}
    for field in fields:
        values = [
            sample["system"][field] for sample in samples
            if sample["system"][field] is not None
        ]
        result[f"avg_{field}"] = round(sum(values) / len(values), 2) if values else None
        result[f"max_{field}"] = max(values) if values else None
    return result


def main():
    args = parse_args()
    try:
        read_process(args.master_pid)
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError) as exc:
        print(f"FAIL: cannot read master PID {args.master_pid}: {exc}", file=sys.stderr)
        return 2

    try:
        log_offset = args.log.stat().st_size
    except FileNotFoundError:
        log_offset = 0

    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    try:
        client = subprocess.Popen(args.command)
    except OSError as exc:
        print(f"FAIL: cannot start load-test command: {exc}", file=sys.stderr)
        return 2
    redis_pids = find_redis_pids()
    clock_ticks = os.sysconf("SC_CLK_TCK")
    previous = {}
    previous_system = None
    previous_time = started
    samples = []

    try:
        while True:
            now = time.monotonic()
            workers = child_pids(args.master_pid)
            targets = [("master", args.master_pid)]
            targets.extend(("worker", pid) for pid in workers)
            targets.append(("client", client.pid))
            targets.extend(
                ("client_worker", pid) for pid in descendant_pids(client.pid)
            )
            targets.extend(("redis", pid) for pid in redis_pids)
            processes, previous = sample_processes(
                targets, previous, now - previous_time, clock_ticks
            )
            current_system = read_system()
            samples.append({
                "elapsed_sec": round(now - started, 3),
                "processes": processes,
                "system": system_delta(
                    current_system, previous_system, now - previous_time
                ),
            })
            previous_system = current_system
            previous_time = now
            if client.poll() is not None:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        client.send_signal(2)
        client.wait()

    queue_events, _ = read_queue_events(args.log, log_offset)
    result = {
        "label": args.label,
        "started_utc": started_utc,
        "duration_sec": round(time.monotonic() - started, 3),
        "interval_sec": args.interval,
        "command": args.command,
        "client_exit_code": client.returncode,
        "master_pid": args.master_pid,
        "initial_redis_pids": redis_pids,
        "samples": samples,
        "process_summary": process_summary(samples),
        "system_summary": system_summary(samples),
        "queue_log_events": queue_events,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Resource samples written to {args.output}")
    return client.returncode


if __name__ == "__main__":
    sys.exit(main())
