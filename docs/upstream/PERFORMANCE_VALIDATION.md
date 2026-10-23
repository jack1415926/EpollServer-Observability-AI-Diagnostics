# 阶段性性能验证

> 最终更新：2026-09-15。本文是性能数据的唯一事实来源；问题发现、排除过程和修复证据见 [压测问题定位记录](PERFORMANCE_TROUBLESHOOTING.md)。

## 测试目标与边界

目标是验证当前修改后的程序和量化 Redis Lua 限流成本，不追求生产级容量结论。

- Ubuntu 24.04.5 VMware VM，12 vCPU、约 3.8 GiB 内存。
- Debug 构建，4 Worker × 120 业务线程，本机 loopback。
- 服务端固定 CPU 0–3，客户端固定 CPU 4–11。
- 6 个 Python 压测进程，总并发 200，每连接 10000 次 Ping，每轮 200 万请求。
- 每组预热 10 万请求，再执行 3 轮；报告三轮中位数。
- 客户端使用 `recv_into` 完整收满 8 字节响应，逐包校验包长、命令码和空包 CRC。
- 使用 Linux `SO_RCVTIMEO`/`SO_SNDTIMEO`，每 100 个请求抽样一次延迟。

这些结果受 VMware 调度和同机客户端影响，不能解释为服务器理论上限。

## 压测客户端校准

旧脚本使用单次 `recv(8)`、只检查长度、共享全局计数和非单调的 `time.time()`，历史约 1.8 万 QPS 不能直接作为严格基准。

早期严格客户端只有约 0.9 万 QPS。A/B 测试发现，完整收包、CRC/字段校验和延迟采样都不是减半主因；Python `socket.settimeout()` 会让每次 I/O 增加用户态就绪等待。连接后改用 Linux 内核收发超时，严格单进程达到约 1.61 万 QPS，同时保留超时失败分类。

客户端最终支持：

- `--processes`：拆分压测进程，不改变总连接数和请求数。
- `--latency-mode all|sampled|off`：延迟全量、抽样或关闭；均不关闭协议校验。
- `--receive-mode into`、`--validation-mode strict`、`--socket-mode kernel-timeout`：当前正式口径。
- `single`、`length-only`、`blocking`：仅用于历史口径诊断，不得用于正式结果。

相同服务端 4 核/客户端 8 核分配下，6 进程长期中位数约 5.31 万 QPS，高于 8 进程的约 4.94 万，因此最终使用 6 进程。

## 最终结果

性能代码基于提交 `9072832`。两组测试开始时工作树均干净；Redis-on 之前只增加了结果文档，服务端和压测代码未变化。

| 模式 | 三轮 QPS | QPS 中位数 | p50 | p95 | p99 | 正式请求 |
|---|---|---:|---:|---:|---:|---:|
| Redis Lua 关闭 | 44312 / 51873 / 54906 | 51873 | 3.00 ms | 6.69 ms | 9.33 ms | 600 万，零失败 |
| Redis Lua 开启，阈值 1 亿 | 23846 / 27893 / 34728 | 27893 | 5.17 ms | 10.59 ms | 14.02 ms | 600 万，零失败 |

两组服务端发送丢弃计数均为 0。Redis-on 额外观测到：

- Redis `commandstats` 记录 610 万次 `EVAL`，对应 10 万预热和 600 万正式请求。
- `failed_calls=0`、`rejected_calls=0`。
- Redis 进程平均 CPU 约 50%–62%。

同口径中位数对比：每请求同步 Redis Lua 使 QPS 下降约 46.2%，p95 上升约 58.3%。这是当前限流设计的同步往返成本，不表示 Redis 异常。

## 复现参数

基线必须复制 `server/nginx.conf` 到临时目录，不能修改并提交默认安全配置。

```ini
# Redis-off
RedisRateLimitEnable = 0

# Redis-on 对照；高阈值只用于避免压测触发封禁
RedisRateLimitEnable = 1
RedisRateLimitWindowSec = 60
RedisRateLimitMaxRequests = 100000000
```

客户端核心参数：

```bash
taskset -c 4-11 python3 testscript/tcp_stress_test.py \
  --host 127.0.0.1 --port 18080 --timeout 3 \
  --concurrency 200 --requests-per-client 10000 --processes 6 \
  --receive-mode into --validation-mode strict --socket-mode kernel-timeout \
  --latency-mode sampled --latency-sample-every 100 \
  --build-mode debug --server-workers 4 --worker-threads 120 \
  --rate-limit-mode disabled --label final-debug-run \
  --json-out /tmp/epoll-benchmark/run.json
```

Redis-on 时将 `--rate-limit-mode` 改为 `enabled`。正式运行由 `performance_sampler.py` 包裹客户端，以同步记录 Master/Worker、客户端子进程、Redis、整机 CPU、上下文切换和服务队列。

## 简历建议口径

> 基于 Linux epoll、Master-Worker 和线程池实现并发 TCP 服务器，完善连接生命周期、优雅退出、MySQL 连接池和 Redis Cache-Aside；重构 Python 压测工具，支持多进程、完整协议校验、内核超时和资源采样。在 12-vCPU VMware 本地环回 Debug 环境下，200 并发 Ping 三轮 QPS 中位数约 5.2 万，累计 600 万请求零失败；开启每请求 Redis Lua 限流后中位数约 2.8 万 QPS，量化同步 Redis 往返约 46% 的吞吐成本。

不应写成“生产吞吐”“稳定上限”或“服务器理论极限”。
