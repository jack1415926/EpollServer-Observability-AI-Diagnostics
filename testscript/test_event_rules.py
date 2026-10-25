"""Compile and execute the production deterministic rule engine without services."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EventRulesTests(unittest.TestCase):
    def test_rules(self):
        with tempfile.TemporaryDirectory(prefix="epoll-event-rules-") as directory:
            binary = str(Path(directory) / "rules")
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-I" + str(ROOT / ".deps/usr/include"), "-I" + str(ROOT / "monitor/collector"),
                            str(ROOT / "monitor/collector/event_rules.cxx"),
                            str(ROOT / "testscript/test_event_rules.cxx"), "-o", binary], check=True, timeout=60)
            subprocess.run([binary], check=True, timeout=5)
