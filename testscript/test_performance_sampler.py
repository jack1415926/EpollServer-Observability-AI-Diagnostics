"""Offline checks for the performance resource sampler."""

import tempfile
import unittest
from pathlib import Path

from performance_sampler import (
    descendant_pids,
    parse_stat,
    parse_status,
    process_summary,
    read_queue_events,
    sample_processes,
    system_delta,
    system_summary,
)


class PerformanceSamplerTests(unittest.TestCase):
    def test_proc_parsing_and_cpu_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            proc_root = Path(directory)
            process_dir = proc_root / "123"
            process_dir.mkdir()
            (process_dir / "comm").write_text("nginx\n", encoding="utf-8")
            (process_dir / "status").write_text(
                "VmRSS:\t2048 kB\nThreads:\t5\n", encoding="ascii"
            )
            (process_dir / "stat").write_text(
                "123 (worker process) S 1 0 0 0 0 0 0 0 0 0 30 20 0 0 0\n",
                encoding="ascii",
            )

            first, ticks = sample_processes(
                [("worker", 123)], {}, 1, 100, proc_root
            )
            self.assertIsNone(first[0]["cpu_percent"])
            self.assertEqual(first[0]["rss_kib"], 2048)
            self.assertEqual(first[0]["threads"], 5)

            (process_dir / "stat").write_text(
                "123 (worker process) S 1 0 0 0 0 0 0 0 0 0 70 30 0 0 0\n",
                encoding="ascii",
            )
            second, _ = sample_processes(
                [("worker", 123)], ticks, 0.5, 100, proc_root
            )
            self.assertEqual(second[0]["cpu_percent"], 100.0)

    def test_queue_log_increment_and_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "error.log"
            log.write_text("old line\n", encoding="utf-8")
            offset = log.stat().st_size
            with log.open("a", encoding="utf-8") as stream:
                stream.write(
                    "2026/09/14 [stderr] 42: 当前收消息队列/发消息队列大小分别为"
                    "(3/4)，丢弃的待发送数据包数量为5。\n"
                )
            events, new_offset = read_queue_events(log, offset)
            self.assertEqual(new_offset, log.stat().st_size)
            self.assertEqual(events[0]["pid"], 42)
            self.assertEqual(events[0]["recv_queue"], 3)
            self.assertEqual(events[0]["send_queue"], 4)
            self.assertEqual(events[0]["discarded_send_packets"], 5)

            summary = process_summary([
                {"processes": [{
                    "role": "worker", "pid": 42, "name": "nginx",
                    "cpu_percent": 25.0, "rss_kib": 100, "threads": 4,
                }]},
                {"processes": [{
                    "role": "worker", "pid": 42, "name": "nginx",
                    "cpu_percent": 75.0, "rss_kib": 120, "threads": 5,
                }]},
            ])
            self.assertEqual(summary[0]["avg_cpu_percent"], 50.0)
            self.assertEqual(summary[0]["max_cpu_percent"], 75.0)
            self.assertEqual(summary[0]["max_rss_kib"], 120)

    def test_individual_parsers_reject_or_default(self):
        self.assertEqual(parse_stat("1 (x y) S 0 0 0 0 0 0 0 0 0 0 2 3"), {
            "ppid": 0, "cpu_ticks": 5, "processor": None,
        })
        self.assertEqual(parse_status("Name:\ttest\n"), {
            "rss_kib": 0,
            "threads": 0,
            "voluntary_context_switches": 0,
            "nonvoluntary_context_switches": 0,
        })
        with self.assertRaises(ValueError):
            parse_stat("invalid")

    def test_descendants_and_system_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            proc_root = Path(directory)
            for parent, children in ((1, "2 3"), (2, "4"), (3, ""), (4, "")):
                task = proc_root / str(parent) / "task" / str(parent)
                task.mkdir(parents=True)
                (task / "children").write_text(children, encoding="ascii")
            self.assertEqual(descendant_pids(1, proc_root), [2, 3, 4])

        previous = {
            "cpu_ticks": [100, 0, 50, 800, 20, 0, 10, 20],
            "context_switches": 1000,
            "processes_created": 100,
        }
        current = {
            "cpu_ticks": [140, 0, 70, 830, 30, 0, 10, 20],
            "context_switches": 1100,
            "processes_created": 104,
            "load_1m": 1.0,
            "load_5m": 0.5,
            "load_15m": 0.25,
        }
        measured = system_delta(current, previous, 2)
        self.assertEqual(measured["cpu_busy_percent"], 60.0)
        self.assertEqual(measured["cpu_steal_percent"], 0.0)
        self.assertEqual(measured["context_switches_per_sec"], 50.0)

        summary = system_summary([{"system": measured}])
        self.assertEqual(summary["avg_cpu_busy_percent"], 60.0)


if __name__ == "__main__":
    unittest.main()
