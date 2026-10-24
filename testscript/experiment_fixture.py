"""Synthetic input for validation tests only; never presented as a real measurement."""
def valid_report():
    return {"name": "validation fixture", "benchmark": {
        "timestamp_utc": "2026-10-24T00:00:00+00:00", "label": "fixture", "git_commit": "a" * 40,
        "git_dirty": False, "system": "test-only", "python": "3", "cpu_count": 4, "memory_total_mib": 1024,
        "target": {"host": "127.0.0.1", "port": 1234},
        "server": {"build_mode": "debug", "workers": 2, "worker_threads": 4, "rate_limit_mode": "enabled"},
        "load": {"concurrency": 2, "requests_per_client": 5, "requested": 10, "processes": 1,
                 "receive_mode": "into", "validation_mode": "strict", "socket_mode": "kernel-timeout",
                 "latency_mode": "all", "latency_sample_every": None, "timeout_sec": 5},
        "result": {"duration_sec": 1, "attempted": 10, "successful": 10, "failed": 0, "not_attempted": 0,
                   "success_rate_percent": 100, "qps": 10,
                   "latency_ms": {"sample_count": 10, "mean": 1, "p50": 1, "p95": 2, "p99": 3, "max": 3}, "errors": {}}},
        "context": {"environment_id": "fixture", "binary_sha256": "b" * 64, "client_sha256": "c" * 64,
                    "server_cpus": "0,1", "client_cpus": "2,3", "snapshot_enabled": True,
                    "redis_window_sec": 60, "redis_max_requests": 100000000, "command": "test-only fixture",
                    "result_sha256": "d" * 64, "resources_sha256": "e" * 64},
        "resources": {"duration_sec": 1, "interval_sec": 0.2, "client_exit_code": 0,
                      "process_summary": [], "system_summary": {}}}
