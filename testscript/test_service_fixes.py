"""Compile and run targeted real-C++ regressions without a running DB/Redis/server."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux C++/MySQL development libraries")
class ServiceFixTests(unittest.TestCase):
    def compile_run(self, name, sources, flags=(), arguments=()):
        with tempfile.TemporaryDirectory(prefix="epoll-service-fixes-") as directory:
            binary = Path(directory) / name
            subprocess.run(["g++", "-std=c++17", "-pthread", "-Wall", "-Wextra",
                            "-I" + str(ROOT / "server/include"), "-I/usr/local/include",
                            *(str(ROOT / source) for source in sources), *flags, "-o", str(binary)],
                           check=True, timeout=90, capture_output=True, text=True)
            subprocess.run([str(binary), *arguments], check=True, timeout=30)

    def test_pool_recovers_and_slow_ping_does_not_hold_pool_lock(self):
        self.compile_run("pool", ["testscript/test_mysql_pool.cxx", "server/misc/ngx_c_mysql_connpool.cxx"],
                         ["-lmysqlclient", "-Wl,--wrap=mysql_real_connect", "-Wl,--wrap=mysql_ping",
                          "-Wl,--wrap=mysql_set_character_set"])

    def test_dao_distinguishes_absence_and_query_errors(self):
        symbols = ["init", "prepare", "close", "bind_param", "execute", "bind_result", "fetch"]
        self.compile_run("dao", ["testscript/test_mysql_dao.cxx", "server/misc/ngx_c_mysql_dao.cxx"],
                         ["-lmysqlclient", *("-Wl,--wrap=mysql_stmt_" + name for name in symbols)])

    def test_cache_round_trip_and_invalid_encodings(self):
        self.compile_run("cache", ["testscript/test_cache_codec.cxx"],
                         ["-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections"])

    def test_receive_queue_message_limit_and_shutdown(self):
        self.compile_run("queue", ["testscript/test_threadpool_limits.cxx", "server/misc/ngx_c_memory.cxx"])

    def test_receive_queue_byte_limit_and_shutdown(self):
        self.compile_run("queue", ["testscript/test_threadpool_limits.cxx", "server/misc/ngx_c_memory.cxx"],
                         arguments=["bytes"])


if __name__ == "__main__":
    unittest.main()
