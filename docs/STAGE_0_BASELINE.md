# 阶段 0：基线确认

日期：2026-10-23。所有命令从**新项目根目录**执行，路径如果有空格需要加引号。

## 当前证据

| 项目 | 状态 |
|---|---|
| 两个上游完整克隆、固定提交 | 已完成，见 SOURCE_BASELINE.json |
| TCP 基础源码导入 | 原样导入，不含旧 Git、Qt 客户端和简历资料 |
| Windows 协议离线检查 | 原克隆 3 项测试通过，耗时 2.246 秒 |
| 新根目录离线回归 | Windows Python 3.12.4：16 项中 15 通过，1 失败，见下方 |
| Ubuntu 构建、真实 TCP/MySQL/Redis 联调 | 待用户执行 |
| 停机、Worker 异常退出 | 待用户执行 |
| 新项目性能基线 | 未执行，不能沿用历史 QPS 当作新结果 |

Windows 首次受限运行出现模拟 TCP 连接超时；相同协议测试在解除执行限制后通过，未修改测试。该结果不证明 Linux 服务可用。

新根目录执行 `python -m unittest discover -s testscript -p 'test_*.py' -v`，耗时 2.949 秒，返回码 1。唯一失败为 `test_tcp_stress.TcpStressTests.test_kernel_timeout_is_classified`：预期 `timeout`，实际 `eof`。已验证 `tcp_stress_test.py:85-94` 使用 `struct.pack('@ll', ...)` 设置内核超时；这是面向 Linux 的 timeval 格式，Windows 平台语义不同是当前推断。没有修改原脚本，也没有将本轮回归标记为全部通过；等待 Ubuntu 上运行同一套测试。

## 下一小批：环境检查与构建

把当前项目复制到 Ubuntu 的本地 Linux 文件系统，如 `~/projects/epoll-observability`。不需要复制 `sources/`，也不要复制 Windows 虚拟环境。建议目录名不带空格：旧 Makefile 使用未加引号的路径，带空格的构建路径尚未验证。

先回传下面两段命令的输出；出现错误就先处理该错误，不继续运行后续集成测试。

```bash
pwd
uname -a
cat /etc/os-release
getconf _NPROCESSORS_ONLN
free -h
command -v g++ make python3 pkg-config
g++ --version
make --version
python3 --version
ldconfig -p | grep -E 'lib(mysqlclient|mariadb|hiredis|redis\+\+)'
```

```bash
make -C server
```

缺失 redis-plus-plus 等依赖时，按实际报错准备安装命令，不在本阶段盲目安装整套新监控依赖。

## 环境就绪后的验收顺序

以下用于后续批次，不要求第一次反馈就全部执行。

1. **离线回归。** 使用现有 Python 环境；若运行 MCP 测试需先在独立虚拟环境安装 `requirements-mcp.txt`。记录 Python 和依赖版本。

   ```bash
   python3 -m unittest discover -s testscript -p 'test_*.py' -v
   ```

2. **线程池排空。** 编译并执行原有专项测试。

   ```bash
   g++ -std=c++17 -pthread -Iserver/include testscript/test_threadpool_shutdown.cxx \
     server/misc/ngx_c_memory.cxx -o /tmp/epoll-test-threadpool-shutdown
   /tmp/epoll-test-threadpool-shutdown
   ```

3. **业务联调。** 在隔离开发数据库中，按 `sql/README.md` 配置 MySQL、Redis 和 `server/nginx.conf`，从 `server/` 启动服务。不要回传密码或配置密钥。测试会注册测试用户，不能对生产数据库运行。

   ```bash
   python3 testscript/test_register_login.py --host 127.0.0.1 --port 8080 --timeout 10
   python3 testscript/test_get_user_info.py --host 127.0.0.1 --port 8080 \
     --exist-id <测试用户ID> --missing-id <不存在的测试ID>
   ```

   尖括号参数需要先替换，不能原样执行。

4. **停机与 Worker 故障。** 根据 `docs/upstream/SHUTDOWN_VALIDATION.md` 执行 `testscript/test_shutdown.py`，记录返回码、残留进程和日志。预期 Worker 意外退出触发整体退出，不以自动补起作为验收标准。

5. **性能基线。** 在功能通过后，参照 `docs/upstream/PERFORMANCE_VALIDATION.md` 选择适合当前虚拟机的同一组参数，至少三轮，保留 JSON、构建模式、代码提交、Worker/线程配置、限流状态、硬件、资源采样与错误数。负载并发、运行量及可接受开销在执行前确认。暂不重复历史 600 万请求规模。

## 结果记录表

| 检查 | 命令/配置/环境 | 结果与返回码 | 证据文件 |
|---|---|---|---|
| 环境与依赖 | 待填写 | 待执行 | 待填写 |
| Linux 构建 | `make -C server` | 待执行 | 待填写 |
| 离线回归 | 待填写 Python 环境 | 待执行 | 待填写 |
| 业务及数据库 | 隔离开发库 | 待执行 | 待填写 |
| 停机与异常退出 | 待填写 | 待执行 | 待填写 |
| 三轮基线 | 待确认参数 | 待执行 | 待填写 |

只有以上验收形成证据后，才把阶段 0 标记为完成，再进入阶段 1 的快照协议设计与实现。
