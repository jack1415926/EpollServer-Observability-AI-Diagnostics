# 当前状态与后续事项

> 快照日期：2026-09-15。本文只保留当前结论；性能方法与完整数据见 [PERFORMANCE_VALIDATION.md](PERFORMANCE_VALIDATION.md)，历史计划见 [OPTIMIZATION_AND_AGENT_ROADMAP.md](OPTIMIZATION_AND_AGENT_ROADMAP.md)。

## 当前定位

- Linux C++17 学习项目，采用 epoll LT、非阻塞 Socket、Master-Worker 和业务线程池。
- 支持 Ping、注册、登录和按 userId 查询；集成 MySQL 连接池、Redis Cache-Aside 与两级限流。
- `epoll_mcp/` 提供三个本机 stdio 只读工具，不进入 C++ 数据面。
- 当前适合展示 Linux 网络编程和并发资源管理，不应描述为生产可用。

## 已完成验证

- Ubuntu 24.04.5、G++ 13.3、12 vCPU VMware、约 3.8 GiB 内存下完成 Debug 构建。
- 16 项 Python 离线回归通过，覆盖 MCP、协议、压测客户端和资源采样器。
- 线程池排空测试通过；SIGTERM、SIGQUIT、SIGINT、Worker 异常退出 4 个停机场景通过。
- 注册、登录、存在/不存在用户查询以及 Redis 正常值/空值缓存完成真实 MySQL、Redis 联调。
- 4 个 Worker 均观测到独立 Redis TCP 连接，验证 Redis 连接池在 fork 后初始化。
- MCP Client 完成工具发现、文档检索、日志读取和真实 C++ Ping；服务停止时返回结构化连接失败。

## 最终阶段性压测

统一条件：Debug、12 vCPU VMware 本地环回、4 Worker × 120 业务线程；服务端固定 CPU 0–3，6 个压测进程固定 CPU 4–11；总并发 200，每连接 10000 次 Ping；客户端使用 Linux 内核收发超时、`recv_into` 完整收包、严格字段校验和 1/100 延迟抽样。

| 模式 | 三轮 QPS | 中位数 | p95 中位数 | 总请求 |
|---|---|---:|---:|---:|
| Redis Lua 关闭 | 44312 / 51873 / 54906 | 51873 | 6.69 ms | 600 万，零失败 |
| Redis Lua 开启，阈值 1 亿 | 23846 / 27893 / 34728 | 27893 | 10.59 ms | 600 万，零失败 |

- Redis-on 共观测 610 万次成功 `EVAL`，对应 10 万预热和 600 万正式请求，无 rejected/failed call。
- 每请求同步 Redis Lua 使 QPS 中位数下降约 46.2%，p95 上升约 58.3%。
- 两组服务端发送丢弃计数均为 0；结果存在明显 VMware 时段波动，应报告中位数而非最好值。
- 性能代码基于提交 `9072832`；测试时工作树为干净状态。原始 JSON 位于临时目录，长期证据以 [性能验证说明](PERFORMANCE_VALIDATION.md) 为准。

单进程早期约 0.9 万 QPS 的主因已定位为 Python 用户态超时 Socket 对每次 I/O 增加就绪等待。改用 `SO_RCVTIMEO`/`SO_SNDTIMEO` 后，严格单进程约 1.61 万 QPS，并保留超时分类。相同 CPU 分配下，6 个压测进程的中位数高于 8 个进程，因此最终采用 6 进程。

## 可复制验证命令

```bash
make -C server
.venv/bin/python -m unittest discover -s testscript -p "test_*.py" -v
python3 testscript/test_shutdown.py --binary ./server/nginx
```

集成测试需要先启动服务以及 MySQL、Redis：

```bash
python3 testscript/test_register_login.py --host 127.0.0.1 --port 8080 --timeout 10
python3 testscript/test_get_user_info.py --host 127.0.0.1 --port 8080 \
  --exist-id 1 --missing-id 99999
```

压测参数和临时 Redis 开关要求见 [PERFORMANCE_VALIDATION.md](PERFORMANCE_VALIDATION.md)。默认 `server/nginx.conf` 必须保持 Redis 限流开启。

## 未完成但不阻塞当前展示

1. 密码仍为明文存储，注册响应仍可能携带密码；公开部署前必须改为带盐哈希并收紧响应。
2. 慢读客户端填满发送缓冲区、ASan/UBSan、TSan 和长时间资源泄漏专项尚未执行。
3. Qt 客户端尚未完成当前 Linux VM 的跨机联调。
4. 外部压测机、Release 和服务器理论上限不在本轮个人项目验证范围。

## 文档入口

- [README](../README.md)：项目介绍与快速启动。
- [性能验证](PERFORMANCE_VALIDATION.md)：最终条件、结果、解释与简历口径。
- [压测问题定位](PERFORMANCE_TROUBLESHOOTING.md)：单进程减半、多进程选择和 Redis 成本的证据链。
- [停机验收](SHUTDOWN_VALIDATION.md)：优雅退出行为和复现命令。
- [优化路线图](OPTIMIZATION_AND_AGENT_ROADMAP.md)：历史优化项与可选后续方向。
- [历史代码评审](code-review-2026-07-06.md)：当时版本的问题快照，不作为当前状态来源。
