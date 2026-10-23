# Repository Guidelines

## Project Structure & Module Organization

`server/` contains the Linux C++17 service. Its modules separate startup and configuration (`app/`), epoll networking (`net/`), request handlers (`logic/`), process and signal management (`proc/`, `signal/`), shared utilities (`misc/`), and headers (`include/`). `qt-client/` is a separate Qt 6 desktop client with sources under `src/` and shared wire definitions under `shared/`. `epoll_mcp/` is a local Python stdio MCP sidecar that reuses `testscript/protocol.py`. Database setup lives in `sql/`, offline and integration checks in `testscript/`, and design or learning notes in `docs/`.

## Build, Test, and Development Commands

Build and run the server on Linux; it depends on pthreads, MySQL client libraries, hiredis, and redis-plus-plus.

```bash
make -C server                 # build server/nginx in debug mode
./server/nginx                 # run using server/nginx.conf
make -C server clean           # remove generated server objects and binary
python3 testscript/test_register_login.py
python3 testscript/test_get_user_info.py
python3 testscript/tcp_stress_test.py
python3 -m unittest discover -s testscript -p "test_*.py" -v
python3 -m pip install -r requirements-mcp.txt
python3 -m epoll_mcp.server
```

The integration scripts expect a reachable server, and database/cache scenarios also require initialized MySQL and Redis. The protocol and MCP unit tests are offline. Build the Windows client with `cmake -S qt-client -B qt-client/build -DCMAKE_PREFIX_PATH=<Qt6-path>` followed by `cmake --build qt-client/build --config Release`.

## Coding Style & Naming Conventions

Match nearby code; no formatter or linter is configured. Use `.cxx` for server implementations and `.h` for headers. Preserve established prefixes: `C` for classes, `m_` for members, `g_` for globals, and `ngx_` for framework types/functions. Keep protocol structures packed and convert all multibyte wire fields between host and network byte order. Qt types use PascalCase filenames such as `TcpClient.cpp`.

## Testing Guidelines

Tests combine standard-library `unittest` checks with executable integration scripts; no coverage target is defined. For MCP changes, run `test_mcp_tools.py` and `test_protocol.py`; for changed service commands, run the focused integration script, then use `tcp_stress_test.py` for networking or concurrency changes. Record service configuration and dependency state when reporting results; do not promote offline checks to real-service, MySQL, Redis, Linux, or performance evidence.

## Commit & Pull Request Guidelines

Recent history uses concise Chinese summaries, with optional prefixes such as `feat:` and `refactor:`. Keep commits focused and use an imperative summary. Pull requests should explain affected modules, configuration or protocol changes, commands run, and linked issues. Include client screenshots only for visible Qt changes; call out schema, dependency, or `nginx.conf` changes explicitly.

## Security & Configuration

Do not commit real credentials or machine-specific addresses. Treat values in `server/nginx.conf` and `sql/init_users.sql` as local development defaults, and keep generated binaries, logs, and build directories untracked. Keep MCP tools read-only: no arbitrary paths, shell execution, service control, configuration changes, or database/Redis writes without a separately reviewed design.
