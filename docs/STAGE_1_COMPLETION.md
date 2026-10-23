# 阶段 1：端到端采集与汇总验收

状态：**按本轮用户确认的基础指标范围，阶段 1 已实现并通过功能验收**（2026-10-24）。Worker 快照、Master 名单、C++ 读取汇总及 CLI 均可脱离 HTTP 独立工作。阶段 2 尚未开始。

用户确认使用 C++ 读取器，先完成基础指标端到端采集，以小规模开关对照记录性能现象；原 200 并发口径的正式开销验收保留到后续。CPU/内存、累计收发与业务/错误计数、MySQL/Redis 内部指标明确未支持，不表示原规划所有候选指标都已完成。原始规划书保留不改写。

## 已实现的能力

| 模块 | 已实现并验证 | 边界 |
|---|---|---|
| Worker 发布器 | 完整进程身份、BOOTTIME/Unix 采样时间、连接数、收发队列长度，约每秒原子发布 | 默认关闭，逐字段读取，不保证同一瞬间一致；无累计计数或速率 |
| Master 发布器 | 预期 Worker 数、成功 fork 且尚未回收的身份名单、running/stopping、回收更新及正常清理 | 生命周期名单，无心跳；不代表 Worker 就绪或 TCP 健康 |
| C++ 读取器 | 显式实例选择、身份核验、缺失/过期/部分失败、有效数据汇总、读取期间变化拒绝 | 同用户、同机本地文件系统；不探测 TCP、数据库或缓存健康 |
| CLI | 单次 JSON 输出与状态退出码，无 HTTP/AI 依赖 | 不修改文件、配置或服务状态；不自动选择最新实例 |

发布器细节与历史验收分别见 [Worker 快照](STAGE_1_WORKER_SNAPSHOT.md) 和 [Master 名单](STAGE_1_MASTER_MEMBERSHIP.md)。C++ 公共入口为 `monitor/collector/collector.h` 中的 `epoll_monitor::Collect(manifest, maxAgeMs)`，第三个 procRoot 参数仅用于测试替身，CLI 固定读取 `/proc`。JSON 解析使用 nlohmann/json，不将 JSON 库加入 TCP 服务器构建依赖。

## 读取和汇总语义

- 必须传入一个明确的 Master 名单路径。文件名、JSON、当前系统 boot ID 和 `/proc` 的 Master PID/启动刻度须一致，不扫描目录猜测当前实例。名单长时间不变不意味着过期。
- 只读取名单声明的 Worker 文件。Worker boot ID、Master 完整身份、Worker 序号/PID/启动刻度以及实际父 PID 必须匹配；退出或僵尸进程、PID 重用、旧实例和孤立快照不参与汇总。
- 默认 Worker 最大年龄为 3000 ms，可选 1–60000 ms。使用 `CLOCK_BOOTTIME`，年龄大于阈值视为过期；未来 BOOTTIME 时间戳无效。Unix 时间仅作记录，不用于过期判断。
- 快照版本、字段类型、非负计数和范围严格校验；布尔值不能冒充计数，浮点数不能冒充整数。文件/目录需归本用户所有且不可由组或其他用户写入，拒绝最终目录与文件符号链接及非普通文件。Master 文件上限 1 MiB、名单上限 4096 Worker，Worker 文件上限 16 KiB；拒绝重复 JSON 键和过深嵌套。
- `collection_ok=false`、缺失、损坏、无权限、身份不符和过期分别返回明确状态。`write_failures_total` 是最近成功快照中记录的历史失败次数，不代表当前文件系统健康。
- `partial_sums` 仅累加本轮 `fresh` 的 Worker；无有效 Worker 时为 null。只有 `running` 且全部预期 Worker 数据有效时 `totals` 才有值。部分数据不足时完整总量为 null，不能把缺失数据填 0。
- 读取后重新核验名单原文、Master 与已接纳的 Worker 身份，并复查新鲜度；发生变化返回 `instance_changed`，清空本轮 Worker 结果和汇总。本实现不是跨进程事务，完成返回后进程仍可能改变，不能承诺同一瞬间的严格全局快照。
- `tcp_health=not_probed` 固定表明未作 TCP 健康探测。`complete` 只代表基础指标完整。SIGSTOP 进程在快照仍新鲜时可参与采样汇总；超过阈值后会被排除，不能把 `fresh` 解释为线程正在运行。

顶层状态和 CLI 退出码：

| status | 含义 | 退出码 |
|---|---|---:|
| complete | running 且全部预期 Worker 为 fresh | 0 |
| partial | running，部分 Worker 有效，完整总量不可得 | 2 |
| stopping | Master 名单进入停止流程；仍可报告部分和，完整总量为 null | 2 |
| unavailable | 名单/身份不可用，或 running 但无有效 Worker | 3 |
| instance_changed | 读取期间身份、名单或新鲜度变化，拒绝本轮汇总 | 3 |

参数错误退出 64，`--help` 退出 0。Worker 行的 `status` 包含 fresh、missing、stale、dead、process_unavailable、identity_mismatch、collection_failed、future_sample、invalid_fields、invalid_json、unsupported_schema、invalid_file、unreadable。顶层 `reason` 给出失败类别；不会输出原始输入正文或配置内容。

## 构建与使用

需要 Linux、C++17 和 nlohmann/json 开发头文件。系统已安装 `nlohmann-json3-dev` 时直接构建；也可像本次一样无需 sudo 地在项目根目录本地解包：

```bash
mkdir -p .deps
(
  cd .deps
  apt-get download nlohmann-json3-dev
  dpkg-deb -x nlohmann-json3-dev_*.deb .
)
make -C server
make -C monitor
```

`.deps/` 和生成的 `monitor/build/` 均不纳入 Git。其他头文件路径可用 `make -C monitor JSON_INCLUDE=/path/to/include`；测试使用相同路径时设置环境变量 `JSON_INCLUDE`。本次下载的是 Ubuntu 包，解包头文件版本为 3.11.3，没有变更系统安装。

采样器在独立临时配置或本机配置中启用后，用对应启动轮次的完整文件名读取，不要在多个名单中自动取第一个：

```bash
./monitor/build/epoll-metrics /absolute/metrics/master-BOOT-PID-TICKS.json
./monitor/build/epoll-metrics /absolute/metrics/master-BOOT-PID-TICKS.json 5000
```

日常 `server/nginx.conf` 仍默认关闭采样；本次验收没有修改其中的数据库设置。发布器写入故障仅记录告警，保留原 TCP 故障策略；故障文件系统仍可能阻塞文件 I/O 或退出等待，本批没有注入真实磁盘满、网络挂载或磁盘挂死，不承诺故障磁盘下有界延迟。

## 功能合并验收

确认 MySQL、Redis 运行后，在根目录执行（各命令须退出成功）：

```bash
make -C server
make -C monitor
PYTHONPATH=testscript python3 -m unittest test_protocol test_tcp_stress test_performance_sampler test_worker_snapshot test_metrics_collector -v
python3 testscript/test_worker_snapshot_live.py --binary ./server/nginx --collector ./monitor/build/epoll-metrics
python3 testscript/test_shutdown.py --binary ./server/nginx
```

| 实际检查 | 结果 |
|---|---|
| Linux 编译 | TCP 服务 Make 成功；C++ CLI 与真实读取器通过 `-Wall -Wextra -Werror` 编译 |
| 30 项回归 | 全部通过，11.195 秒：协议 3、压测客户端 5、资源采样器 4、发布器 6、C++ 读取器 12 |
| 读取器测试 | 有效和、未声明文件排除、旧 boot/PID/启动刻度、父 PID 不符、僵尸、缺失/损坏/无效类型、采集失败、未来/过期样本、Unix 时钟无关性、文件安全限制及重复字段通过 |
| 并发读取 | 用受控测试替身暂停真实读取器，分别改变名单或使先前已读 Worker 成为僵尸；两者均返回 instance_changed，丢弃整轮汇总 |
| CLI | help、错误年龄/参数及 unavailable 退出码检查通过；0/2/3 与真实联调状态一致 |
| enabled 联调 | 连接数 0→1→0，发布失败时 Ping 正常且不生成假总量，恢复后 complete，SIGSTOP 导致 partial/stale，SIGCONT 后 complete，正常停止清理 |
| invalid-directory / disabled | 初始化失败时 TCP 正常；默认关闭不生成目录 |
| worker-crash | 保留整体退出策略，停止中不报告 complete，退出后不可用 |
| restart-isolation | 真实异常退出留下旧 Worker 文件；新实例汇总仅含当前两 Worker，旧文件保留但被忽略 |
| 停机回归 | SIGTERM、SIGQUIT、SIGINT、Worker 异常退出共 4 场景通过，Worker 回收并释放端口 |

最终端到端日志：`/tmp/epoll-snapshot-live-f2moogpu`；停机回归日志：`/tmp/epoll-shutdown-gdepkqh6`。归档目录 `artifacts/stage1-completion-20261024/` 保存构建、30 项回归、CLI 检查、脱敏服务日志、实际 complete/partial/unavailable/stopping JSON、源码与二进制校验值；配置副本已删除。Git 基点为 `32d5e6c`，本批基于未提交工作树验收，不能表述为干净提交。

MCP 离线测试未纳入本阶段 TCP/指标验收（本机未安装可选 mcp 依赖），不记为通过。也没有重做注册账号或原大规模基线测试。

## 小规模采样开关对照

复现入口：

```bash
python3 testscript/benchmark_worker_snapshot.py --binary ./server/nginx --output ./artifacts
```

本次目录：`artifacts/stage1-completion-20261024/small-ab-4cex820j/`。同一个 Debug 二进制，只改变快照开关；独立临时配置与端口，2 Worker × 4 业务线程，服务端固定 CPU 0–1、客户端 CPU 2–3，Redis Lua 开启、窗口 60 秒、阈值 1 亿。客户端 2 进程、20 并发、每连接 3000 Ping；strict/into/kernel-timeout，超时 5 秒，延迟每 100 次抽样。每轮预热 1 万次，再正式 6 万次，顺序 off/on/on/off/off/on，每种条件 3 轮；每轮均记录资源采样，开启组确认发布器多次采样，退出与快照清理通过。

| 轮次 | 采样 | 成功 | 失败 | QPS | 抽样 P95 ms |
|---|---|---:|---:|---:|---:|
| 1 | off | 60000 | 0 | 23536.105 | 1.372 |
| 2 | on | 60000 | 0 | 23692.005 | 1.232 |
| 3 | on | 60000 | 0 | 25282.805 | 1.210 |
| 4 | off | 60000 | 0 | 24494.280 | 1.285 |
| 5 | off | 60000 | 0 | 21470.429 | 1.460 |
| 6 | on | 60000 | 0 | 27799.027 | 1.235 |
| 中位数 | off | — | — | 23536.105 | 1.372 |
| 中位数 | on | — | — | 25282.805 | 1.232 |

正式共 36 万次成功，另预热共 6 万次成功，均零失败。开启组 QPS 中位数较关闭组高 7.42%，抽样 P95 中位数低 10.20%。这里只描述本批小规模观测；同机 VMware 运行波动及负载口径不同，不能认定采样带来提速、开销为零或原 200 并发下无回归，也不能与阶段 0 的 32203.656 QPS 直接比较。测量的是发布器开关，读取器没有在负载期间持续运行；未来 HTTP/读取负载的开销需另测。

## 完成与后续边界

本阶段已满足确认后的功能完成标准：不依赖 HTTP，可准确区分并取得多 Worker 的结构化基础运行指标。CPU/内存、累计网络/业务/错误计数、MySQL/Redis 内部指标尚未实现，输出通过 unsupported_metrics 明示。按用户确认，原负载口径的正式开销阈值与对照测试留待后续，不阻塞本次功能收尾。

阶段 0 原始目录 `artifacts/baseline-20261023-223210` 保留，文件校验与前批清单一致。未扩展 HTTP、Web 或 AI。用户已授权将阶段 1 的代码、测试与文档提交并推送 GitHub；本地数据库配置、依赖和验收产物排除在提交之外。下一阶段为独立 Drogon HTTP 监控服务，可复用本 C++ 读取器，并将文件读取安排到事件循环之外。
