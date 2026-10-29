# 阶段 1 第二批：Master Worker 名单

历史批次：本页仅记Master名单；后续读取汇总与HTTP已交付。当前状态见[统一入口](CURRENT_STATUS.md)，原验收记录保留。

状态：已实现并于 2026-10-24 通过 Linux 编译及下述测试。范围只包括 Master 生命周期名单发布；读取汇总、旧实例排除与过期判定尚未实现，整个阶段 1 未完成。

以上为第二批历史状态。第三批已补齐 C++ 读取汇总并完成阶段 1 功能验收，当前完整进度、构建与 5 场景端到端验收见 [阶段 1 收尾](STAGE_1_COMPLETION.md)。

## 配置与身份

沿用 `WorkerSnapshotEnable` 和 `WorkerSnapshotDirectory`，默认关闭，不新增配置项。启用后，Master 在全部 fork 尝试结束后创建 `CMasterSnapshot`，不在 fork 前启动线程或打开发布目录。复用 Worker 的目录权限检查和原子文件发布函数；新建目录 0700、文件 0600，拒绝最终目录为符号链接、非本用户所有或组/其他用户可写的情况。

名单文件为 `master-{boot_id}-{master_pid}-{master_start_ticks}.json`，同目录临时文件完整写入并关闭后原子 rename，不承诺 fsync 持久化。多个实例可使用同一目录，各自文件按完整身份区分，没有指向“当前实例”的全局文件；读者不能按文件时间或 PID 大小猜测当前实例。

| 字段 | 含义 |
|---|---|
| `schema_version` | Master 名单协议版本 1；与 Worker 快照是两种独立记录 |
| `boot_id` | Linux 系统启动 ID |
| `master_pid`、`master_start_ticks` | Master PID 和 `/proc/{pid}/stat` 字段 22 |
| `sequence` | 本发布器格式化尝试序号，失败可跳号 |
| `write_failures_total` | 此前发布失败次数，后续成功发布时可见 |
| `state` | `running` 或 `stopping`，仅描述 Master 是否已开始整体退出 |
| `expected_workers` | 配置要求的 Worker 数量，不能用名单长度替代 |
| `workers` | 成功 fork 且尚未由 Master 回收的 Worker 身份列表 |
| `workers[].worker_index` | 原始 fork 序号；fork 失败不会使后续序号前移 |
| `workers[].pid`、`process_start_ticks` | Worker PID 和进程启动刻度；全局身份还需结合 `boot_id` |

名单不是健康检查。Worker 在初始化 MySQL、Redis 或采样器前就可能进入名单；已退出但尚未被 Master 回收的进程也可能短暂存在其中。`running` 不证明 Worker 已就绪、TCP 可达或后端健康。Worker 快照必须同时匹配 Master 完整身份和名单中的 Worker 完整身份，不能只匹配序号或 PID。

## 更新与故障语义

- 初次名单在全部 fork 尝试结束后发布；fork 启动失败或没有 Worker 时为 `stopping`。
- Master 开始处理停机时发布 `stopping`，每批 `waitpid` 回收后更新尚未回收的名单；保留既有 Worker 异常退出导致整体退出的策略，不自动补起。
- 最后一个 Worker 回收后尝试发布空名单，Master 返回时删除自己的最终文件和临时文件。不会清理其他实例或被 SIGKILL Worker 留下的快照。
- 身份读取或目录初始化失败：一次告警，关闭本 Master 名单发布，TCP 按原有流程运行；Worker 发布器仍各自独立初始化。
- 写入失败：保留上次成功文件、累计失败、首次告警；下一次生命周期更新再重试，恢复时记录恢复日志。本批没有周期重试线程或名单心跳，长时间不变的名单不因此判定过期。
- SIGKILL/崩溃可能残留名单。读取器必须核验 boot ID、进程启动刻度和进程状态，名单文件存在不等于服务存活。后续读取器还需将名单发布缺失/失败与 Worker 快照缺失/过期分别处理。

Master 只在生命周期路径执行本机文件 I/O，不在 Worker 的 epoll 收发路径增加文件操作。故障文件系统仍可能阻塞 Master 的启动、回收或停机；本批不承诺磁盘故障下有界退出，不应使用网络挂载目录。

## 合并验收

MySQL 和 Redis 使用已有本机开发服务，先检查两者 active，然后在项目根目录执行：

```bash
make -C server
python3 -m unittest discover -s testscript -p 'test_worker_snapshot.py' -v
python3 testscript/test_worker_snapshot_live.py --binary ./server/nginx
python3 testscript/test_shutdown.py --binary ./server/nginx
```

当前发布器测试预期 6 项，快照联调 4 场景，停机回归 4 场景。临时配置含本机数据库设置，测试退出时会删除，不能归档或上传；日志和结果也应检查脱敏。测试不新增业务用户，只产生短期限流键。

## 实际验证记录

基于 `32d5e6c` 加本批未提交修改，沿用 Linux C++17 Debug；MySQL、Redis 检查均为 active。没有修改本机数据库配置，没有重复阶段 0 的大规模压测。

| 验收项 | 结果 |
|---|---|
| Make 编译链接 | 退出码 0；保留既有注释 Unicode 方向控制字符警告 |
| 发布器测试 | 6/6，4.412 秒，退出码 0；真实 C++ 发布器及探针使用 `-Wall -Wextra -Werror` 编译 |
| Master 探针 | 核对进程启动刻度、文件名、权限、名单内容；重复替换期间解析、停止状态、空名单、发布失败后恢复、正常清理及崩溃残留身份隔离通过 |
| enabled | 双 Worker 名单与各自快照完整身份一致；原 Worker 写入失败/恢复及 Ping 正常；正常停机可观测 `stopping`，最终文件全部清理 |
| invalid-directory | Master 和两个 Worker 共 3 条初始化告警，原目标未改动，Ping 及正常退出通过 |
| disabled | 不创建快照目录，Ping 及正常退出通过 |
| worker-crash | 暂停兄弟 Worker 后 SIGKILL 另一 Worker；观察到名单移除已回收 Worker 且状态为 `stopping`，恢复兄弟后整体退出码 1；只留下被 SIGKILL Worker 的快照，无 Master 名单残留 |
| 原停机回归 | SIGTERM、SIGQUIT、SIGINT、Worker 异常退出共 4 场景通过，Worker 已回收、端口释放 |

最终快照联调日志：`/tmp/epoll-snapshot-live-ujrvceje`；停机回归日志：`/tmp/epoll-shutdown-fzw03xn_`。本机归档 `artifacts/stage1-master-20261024/` 包含构建日志、发布器测试输出、脱敏联调日志、验收摘要、二进制及源码校验值；按仓库规则不入 Git。

待验证：fork 系统调用失败、真实磁盘满/故障、不同用户属主目录未作真实故障注入；已有文件、符号链接和可写目录拒绝以及 rename 失败已覆盖。监控开销仍未测量。下一批为不依赖 HTTP 的名单与快照读取汇总，需验证旧实例、身份不匹配、数据缺失、过期和部分采集失败，不把异常残留当作当前存活进程。
