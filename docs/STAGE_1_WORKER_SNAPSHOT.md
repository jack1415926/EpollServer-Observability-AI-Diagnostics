# 阶段 1 第一批：Worker 基础快照

历史批次：本页仅记Worker快照首批；后续Master、汇总、HTTP与资源观测均已交付。当前状态见[统一入口](CURRENT_STATUS.md)，原验收记录保留。

状态：代码已实现；2026-10-24 已在 Linux 工作区直接通过编译、4 项发布器测试及 3 场景真实 TCP 联调。监控开销尚未验证，整个阶段 1 尚未完成。

以上为第一批历史记录。第二批已增加 Master 名单；第三批已完成 C++ 读取汇总及阶段 1 功能验收，当前为 6 项发布器测试、12 项读取器测试和 5 场景端到端联调。最新状态与合并验收见 [阶段 1 收尾](STAGE_1_COMPLETION.md)。

## 范围与启用

本批只交付 Worker 文件快照，不含 Master 名单发布、读取聚合器、过期判定、CPU/内存、累计业务计数、HTTP 或 AI。`server/misc/ngx_worker_snapshot.cxx` 会被原 Makefile 的 `*.cxx` 规则自动纳入构建，不需要迁移构建系统。

```ini
WorkerSnapshotEnable = 0
WorkerSnapshotDirectory = worker-metrics
```

默认保持关闭，缺少配置项时也关闭；值为 1 时启用。目录相对于启动时的工作目录，支持指定已存在父目录下的绝对路径，不递归创建父目录。验收脚本在独立临时配置中开启，不修改日常配置或数据库密码。

每个已初始化的 Worker 启动一个采样线程。它先发布首份快照，此后每次发布完成后等待 1 秒，因此是约每秒采样，不是严格的一秒时钟。线程只读三个现有原子计数，不获取网络锁、不遍历队列，也不在收发包路径执行文件操作。

## 身份和字段（schema_version=1）

| 字段 | 含义 |
|---|---|
| `schema_version` | 当前协议版本 1 |
| `boot_id` | Linux 当前系统启动 ID |
| `master_pid`、`master_start_ticks` | Master PID 和 `/proc/{pid}/stat` 字段 22 |
| `worker_index` | fork 时的 Worker 序号，从 0 开始 |
| `pid`、`process_start_ticks` | Worker PID 和进程启动时钟刻度 |
| `sampled_at_unix_ms` | UTC Unix 毫秒采样时间，可能受系统时钟调整影响 |
| `sampled_at_boottime_ms` | Linux BOOTTIME 毫秒时间，供同机后续读取器判断年龄 |
| `sequence` | 本发布器的序列号，格式化成功的发布尝试递增，失败可能造成跳号 |
| `collection_ok` | 当前三个原子字段已读取；不代表 TCP、MySQL 或 Redis 健康 |
| `write_failures_total` | 本发布器此前的失败次数，后续成功快照可看到它 |
| `connections_current` | 已接受且尚未走关闭计数路径的 TCP 连接数，不是已登录用户数 |
| `receive_queue_messages` | 业务线程池待取任务数，不含已出队处理中的任务 |
| `send_queue_messages` | 发送队列尚未取出的消息数，不等于内核发送缓冲区或全部未完成发送 |

原子字段分别读取，整份快照不保证来自同一瞬间，不能据其推导严格的跨字段不变量。启动刻度用于身份匹配，不是 Unix 时间；文件身份和 Master 实例身份均须结合 boot ID、PID、启动刻度，不能只依赖 Worker 序号或 PID。

## 文件发布、失败与退出

文件名为 `worker-{boot_id}-{pid}-{process_start_ticks}.json`。临时文件在同一目录以排他方式创建，完整写入并关闭后使用 `renameat` 原子替换目标。读者每次重新打开最终文件，可以读到旧版或新版完整 JSON；`.tmp` 不是快照。

目录新建权限 0700，文件 0600；已有目录必须属于当前有效用户，不能允许组或其他用户写入。拒绝最终目录本身是符号链接。监控读取器初版预期以同一用户运行，不支持任意可写共享目录。

- 启动时配置目录或身份读取失败：记一次告警，关闭本 Worker 的采样，TCP 继续启动；修正后需重启该 Worker 才重新初始化。
- 运行时写入失败：保留上一份成功快照，累计失败，每次发布结束后约一秒重试；持续失败仅在首次告警，恢复时再记录恢复日志。
- 正常退出：先停止接收，唤醒并 join 采样线程，删除本发布器的最终文件及临时文件，再销毁业务资源。
- SIGKILL 等异常退出：旧文件可能残留，且本批没有 Master 名单或读取器，不能把目录中所有文件当作当前存活 Worker。下一批再实现活跃实例匹配与过期判断。

原子替换保证文件可见性的完整性，没有 fsync 持久化承诺。线程隔离避免正常采样等待网络锁，但不保证故障文件系统上的 I/O 永不阻塞；join 也可能等待正在执行的文件 I/O。初版仅用于本机文件系统，不应将目录放在网络挂载上。

## 一组合并验收命令

拿到本次代码后，在 Ubuntu 项目根目录执行，MySQL 和 Redis 使用已验证的本机开发环境：

```bash
(
set -e
make -C server
python3 -m unittest discover -s testscript -p 'test_worker_snapshot.py' -v
python3 testscript/test_worker_snapshot_live.py --binary ./server/nginx
)
printf '本批验收退出码：%s\n' "$?"
```

发布器测试会在临时目录编译真实 C++ 发布器与小型探针，不依赖数据库；覆盖身份、计数变化、读取期间替换、权限拒绝、写入失败恢复、正常退出清理和异常退出残留。预期四项通过。

真实联调会依次启动三个独立双 Worker 实例，使用临时端口和独立进程组：`enabled`（连接数变化、运行中写入失败时 Ping 仍正常、恢复和退出清理）、`invalid-directory`（初始化失败时 Ping 仍正常）、`disabled`（不生成快照）。预期三项 PASS。脚本读取当前 binary 旁的 `nginx.conf` 获取数据库配置，不修改它，不新增业务用户；只在 Redis 留下短期限流计数。临时目录打印为 `Test logs:`，复制的配置在退出时删除，日志保留。

失败时停止后续命令并回传错误及日志路径；不要回传密码。本批不要求重复 600 万次压测，新增监控的性能影响尚待后续同口径开关对照验证。

## 初始 Windows 验证记录（历史）

- 新 Python 测试脚本语法检查通过。
- 既有协议 3 项、资源采样器 4 项离线回归通过，共 7 项；未将这些结果作为新增 C++ 发布器的运行证据。
- Windows MinGW 对新增 C++ 公共头文件与测试探针的 `-fsyntax-only -Wall -Wextra -Werror` 检查通过；没有编译 Linux 发布器实现或完整服务器。
- 四项发布器用例在 Windows 按平台跳过，并非运行通过。
- 当时 Linux 编译、四项发布器测试、三场景真实联调以及监控开销尚待验收；Linux 运行结果见下节。

## Linux 合并验收记录（2026-10-24）

验收代码：`32d5e6c`，与更新后的 `origin/main` 一致。本机 `server/nginx.conf` 的数据库配置已私密备份并保留，未输出或提交密码；远端新增的快照默认设置已合入。工作树包含该本地配置修改，不能描述为干净工作树。

环境：G++ 15.2.0、Python 3.14.4、Linux `7.0.0-38-generic`，沿用 Make 的 C++17 Debug 构建。MySQL 检查为 active；Redis 初始为 inactive，启动已有 `redis-server` 开发服务后执行联调，未重置数据库或缓存。测试使用临时端口、双 Worker、每 Worker 4 个业务线程，数据库连接配置取自本机文件。

| 验收项 | 实际结果 |
|---|---|
| `make -C server` | 退出码 0，新增发布器参与编译及链接；保留既有注释 Unicode 方向控制字符警告 |
| 动态库检查 | `ldd server/nginx` 无缺失项 |
| 发布器测试 | 4/4 通过，耗时 3.802 秒，退出码 0；探针与真实发布器通过 `-Wall -Wextra -Werror` 编译 |
| enabled | PASS：双 Worker 身份、连接增减、写入失败时 Ping 正常、恢复发布、正常退出清理 |
| invalid-directory | PASS：初始化告警、目标文件保持原文、Ping 正常、正常退出 |
| disabled | PASS：Ping 正常，不创建快照目录，正常退出 |

合并验收按文档顺序逐项执行并检查退出码，全部通过，无需修改代码。首次在受限沙箱执行真实联调因创建 socket 被拒绝（EPERM）而未启动服务；获准在沙箱外重跑后三场景全部通过，该权限限制不是服务器缺陷。

真实联调日志：`/tmp/epoll-snapshot-live-lhyix6nl`；临时配置已删除。构建日志、验收结果摘要、脱敏联调日志、环境及二进制 SHA256 归档于本机 `artifacts/stage1-acceptance-20261024/`（按仓库规则不入 Git）。二进制 SHA256：`7f4481641c2829e4d0f1f2ed7eb1e88be429cdf807977334df48f60b2be2b825`。阶段 0 原始目录 `artifacts/baseline-20261023-223210` 保留未改动；本次归档附有其文件校验清单，未重复 600 万次 Ping 压测。

已实现且已验证：本批 Worker 基础快照及上述正常、降级、恢复、清理场景。待验证：同口径开关对照的监控开销，本批不作性能无回归承诺。尚未实现：Master 活跃 Worker 名单、读取与汇总、旧实例和过期判定，其验收留待后续小批；HTTP、Web、AI 不在本批范围。
