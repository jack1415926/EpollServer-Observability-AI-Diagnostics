# 阶段 2：独立 Drogon HTTP 监控

历史阶段：本页四接口与未实现事件的描述保留阶段2范围；事件/Web/诊断及CPU/RSS后续已交付。当前状态见[统一入口](CURRENT_STATUS.md)，最新开销见[正式评估](MONITOR_OVERHEAD.md)。

2026-10-24，本批完成功能验收。范围按 `DECISIONS.md` 调整：只交付 health、overview、workers、metrics；事件检测及查询在阶段 4 实现。

## 结构与采样语义

`monitor/http/main.cxx` 构建为独立 `epoll-monitor` 进程，复用阶段 1 的真实 C++ `Collect`。不链接 TCP 服务，不读业务数据库配置，也不接受 HTTP 指定文件路径。启动时必须给出一个明确的 Master 名单绝对路径；TCP 重启后需用新名单重新启动监控，不自动选择目录中的实例。

单个后台线程串行读取快照，每轮结束后等待 500 ms。HTTP 的两个 I/O 线程只读取有锁保护的内存结果，不为每个请求启动采集或排队读取磁盘。身份、名单与文件权限核验沿用读取器；HTTP 进程须与快照发布者属于同一用户。

Worker 样本最多有效 3000 ms。内存缓存期限从本轮文件读取**开始前**计算，取 1500 ms 与所有 fresh Worker 剩余有效时间的最小值。使用 Linux `CLOCK_BOOTTIME`，不受 Unix 时钟校准影响，也不因系统休眠延长有效期。超过期限返回 `collection_timeout`，丢弃完整总量、部分和与 Worker 列表。慢读取完成后也不能为旧结果重新续期；下一轮有效采集恢复后自动恢复。

这是周期采样，进程退出或名单变化在下一轮才会反映，最多可在上述缓存期限内报告先前有效结果。响应 `timestamp_unix_ms` 是生成响应的时间；`data.collected_at_unix_ms` 及 Worker `age_ms` 是采集时的信息，不随每次请求改写。沿用阶段 1 的非事务快照语义。

`tcp_health=not_probed`表示未启用主动探测。2026-10-31已增加[主动TCP Ping](TCP_ACTIVE_PROBE.md)：配置本机TCP端口后，HTTP返回缓存的reachable/unreachable/unknown状态及耗时、时间和原因。`complete`表示基础指标采集完整，health的HTTP 200表示监控进程存活；Ping结果单独读取。

## 构建与启动

Ubuntu 安装开发依赖后：

```bash
sudo apt-get install libdrogon-dev libjsoncpp-dev nlohmann-json3-dev
make -C monitor
make -C monitor http
```

保留 Make，不迁移 TCP 工程。HTTP 是可选构建目标，原 `make -C monitor` 仍只构建 CLI。构建参数支持 `JSON_INCLUDE` 和 `DEPS_PREFIX`，默认优先使用忽略目录 `.deps/usr`。

本机无交互式 sudo 密码，本次使用 Ubuntu 包本地解包：Drogon 1.9.11、Trantor 1.5.24、JsonCpp 1.9.6；未修改系统安装。复现同一方式：

```bash
mkdir -p .deps
(
  cd .deps
  apt-get download libdrogon-dev libdrogon1t64 libtrantor-dev libtrantor1 \
    libjsoncpp-dev libjsoncpp26 libhiredis1.1.0 libyaml-cpp0.8 libcares2 libmariadb3
  for package in *.deb; do dpkg-deb -x "$package" .; done
)
make -C monitor http
```

包名随 Ubuntu 版本变化；以上针对本次 resolute 环境。本地库的运行时路径相对于 `monitor/build/epoll-monitor` 指向 `../../.deps/usr/lib/<编译器架构>`，使用系统库时可正常回退；若把二进制移出项目或使用其他本地依赖目录，需要配置系统库路径或 `LD_LIBRARY_PATH`。

先按阶段 1 文档启用隔离 TCP 实例的快照。令牌从环境读取，支持 32～256 个字母、数字或 `-._~+/=`，不接受默认令牌、不放在命令行参数中：

```bash
read -rsp 'Monitor token: ' EPOLL_MONITOR_TOKEN
export EPOLL_MONITOR_TOKEN
./monitor/build/epoll-monitor /absolute/metrics/master-BOOT-PID-TICKS.json 8080
```

另一个已设置同一令牌的终端中查询：

```bash
curl -sS -H "Authorization: Bearer ${EPOLL_MONITOR_TOKEN}" \
  http://127.0.0.1:8080/api/v1/overview
```

参数错误退出 64，`--help` 退出 0；SIGTERM 正常退出。端口默认 8080，仅绑定 `127.0.0.1`，本批没有对外网绑定或 TLS 配置入口。

## 接口契约

所有应用响应使用 JSON：`schema_version=1`、`timestamp_unix_ms`、`code`、`validity`、`data_valid`、`data`，带 `Cache-Control: no-store`。`data_valid=true` 仅出现在 `complete`，包括 health 中也指基础指标完整性；health 自身存活看 `data.monitor`。

| GET 路径 | data |
|---|---|
| `/api/v1/health` | monitor=ok、tcp_health、tcp_probe、collection_status、reason；TCP探测默认未启用 |
| `/api/v1/overview` | 读取结果去掉 Worker 明细，保留 Master 名单、有效数量、完整总量与部分和 |
| `/api/v1/workers` | 读取结果去掉 totals/partial_sums，保留 Worker 身份、状态与基础指标 |
| `/api/v1/metrics` | 读取结果去掉 master/workers，保留有效数量、完整总量、部分和及 unsupported_metrics |

`connections_current` 是当前 TCP 连接数，不是已登录用户数。三个已支持指标为当前连接、接收队列条数、发送队列条数；CPU/内存、累计网络/业务计数及 MySQL/Redis 内部指标继续列入 `unsupported_metrics`。

| 场景 | HTTP 状态与正文 |
|---|---|
| complete | 200；code=ok；data_valid=true |
| partial / stopping | 200；data_valid=false；完整总量各字段为 null，有效部分和明确标为 partial_sums |
| unavailable / instance_changed / 缓存超时 | overview/workers/metrics 为 503；health 为 200，collection_status 和 reason 明示失败 |
| 无令牌或令牌错误 | 401；WWW-Authenticate: Bearer |
| 未实现路径，含 events | 404，无静态文件下载 |
| 已实现路径使用非 GET 方法 | 405 |
| GET 含 query 或请求体 | 400，不接受运行时切换实例等参数 |
| 全局限流 | 429；Retry-After: 1 |

限流令牌桶为每秒 100 次、初始/最大突发 100 次，认证失败也消耗额度；全局固定大小状态，不按 IP 无界建表。Drogon 限制总连接/单 IP 连接为 64，两个 I/O 线程，请求体最多 1024 字节、全部保存在内存，空闲超时 5 秒、每连接最多 100 请求、流水线最多 4 请求。连接上限可能直接断开连接。

**解析器边界：** 请求体超限在 Drogon 应用处理前返回 413，可能没有 JSON 正文；其他无效 HTTP 报文也可能直接断开。客户端需先处理 HTTP 状态和空正文，再解析应用 JSON。应用路由及鉴权等错误保持统一 JSON，框架可转交应用的错误也使用相同封装。

## 验收与证据

```bash
make -C server
make -C monitor
make -C monitor http
PYTHONPATH=testscript python3 -m unittest test_protocol test_tcp_stress \
  test_performance_sampler test_worker_snapshot test_metrics_collector \
  test_service_fixes test_http_monitor -v
python3 testscript/test_http_monitor_live.py
git diff --check
```

2026-10-24 实测：

| 检查 | 结果 |
|---|---|
| 编译 | TCP、CLI、Drogon 服务成功；HTTP 以 C++17、-Wall -Wextra -Werror 构建 |
| 合并回归 | 45 项通过，25.004 秒；原回归 37 项 + HTTP 8 项 |
| HTTP 测试 | 使用真实 /proc 身份与受控文件；四接口、总量、鉴权、路径/输入/静态文件拒绝、413、部分/损坏/过期/缺失/死亡/停止/恢复、限流恢复、启动参数校验通过 |
| 慢采集注入 | 仅暂停后台读取五秒，缓存到期后返回 collection_timeout；health 在一秒内响应，随后重新采集恢复 complete |
| 真实 TCP 联调 | 独立 2 Worker 实例，HTTP 连接数 0→1→0；SIGSTOP 导致 partial 且无假总量，SIGCONT 恢复 complete |
| 监控失效 | 名单权限错误时 HTTP 503，TCP Ping 正常；SIGKILL HTTP 后 TCP Ping 仍正常 |
| TCP 失效 | Worker SIGKILL 保留 TCP 整体退出策略；HTTP 继续运行，overview 503，health 200 且不谎报 TCP 健康；HTTP SIGTERM 正常退出 |

真实联调目录 `/tmp/epoll-http-live-rgha5ddg`；配置副本已删除，测试创建的进程已回收。`artifacts/stage2-http-20261024/` 保存 complete、连接数为 1、partial、名单权限失败、TCP 退出和对应 health 的真实 JSON，以及验收摘要。产物和本地依赖不纳入 Git。基于包含上一批审查修复的未提交工作树执行，不能表述为某个干净提交的结果。

首次原回归在沙箱中因本机 socket 被禁止而失败，随后获准在沙箱外完整重跑，45 项全部通过；这不是代码回归失败。

本批没有创建业务用户或修改业务数据，没有重跑大规模压测、HTTP 持续负载下的正式开销对照、可选 MCP、全服务 sanitizer 或真实磁盘挂死。单采集线程避免请求无限排队，但不能取消内核中阻塞的文件 I/O；真实故障磁盘下正常退出可能等待该线程，不承诺有界退出时间。未实现主动 TCP 健康探测、事件、Web、实验持久化或 AI。

Drogon 接口参考：[HttpAppFramework 官方头文件](https://github.com/drogonframework/drogon/blob/v1.9.11/lib/inc/drogon/HttpAppFramework.h)。
