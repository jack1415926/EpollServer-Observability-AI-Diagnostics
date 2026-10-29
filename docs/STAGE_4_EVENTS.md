# 阶段 4：基础异常检测与事件

阶段4功能已交付；本页保留当批验证与规则契约。当前资源/诊断及开销状态见[统一入口](CURRENT_STATUS.md)，后续评估不改写本页历史成绩。

2026-10-25，按用户确认实现三类已有数据规则、MySQL 持久化、自动恢复、查询 API 与网页列表。不扩展业务计数、数据库/Redis 插桩或 TCP 主动探测，不提供人工确认、关闭或通知。

## 规则与证据

规则在独立线程每约 500 ms 读取已有采集缓存，不在 TCP 收发热路径或 Drogon 网络线程执行。采集缓存过期仍失效，不续用旧总量。

| 规则 | 触发条件 | 恢复证据 |
|---|---|---|
| collection_unavailable | 非初始化、非正常停止状态，基础采集不完整，包括缺失、过期、损坏、身份变化、缓存超时 | 全部预期 Worker 数据重新完整 |
| worker_process_anomaly | 名单中具体 PID/启动刻度的进程已退出，或身份/父进程不匹配 | 同一身份的 Worker 恢复有效采样 |
| receive_queue_backlog / send_queue_backlog | 单 Worker 对应队列条数持续大于等于配置阈值 | 该 Worker 有效样本的对应队列低于阈值 |

默认连续异常 3000 ms 触发，连续正常 3000 ms 恢复。新鲜度上限仍为 3000 ms，因此停止采样到触发事件还包含样本变旧的时间。缺失数据为未知，不能认为队列归零或业务正常；未知会中断连续窗口。检测间隔超过 1500 ms 或单调时钟回退时重新计时，不把未观察的间隔算作持续异常。

进程访问失败只属于采集异常，不等同于进程退出。快照身份字段不符单独标为 snapshot_identity_mismatch，只触发采集异常；identity_mismatch 专指实际进程身份或父进程不符，可作为 Worker 进程异常证据。快照或 Master 名单消失不能单独证明 Worker 非预期退出。保留原有 Worker 故障触发整个 TCP 服务退出的策略，因此快速整体退出可能只留下采集不可用事件；不会编造 Worker 退出原因。停止中的 Master 不触发新事件或误恢复已有事件；本监控进程已观察到 stopping 后，名单删除仍保持停止抑制，直至再次有效观察到 running。监控重启后没有历史停止观测时，缺失名单仍属于采集不可用。

事件状态仅 `active` / `resolved`。active 表示已触发且尚无恢复证据，不保证当前仍异常；实例消失或正常停止不会伪造恢复。事件包含 UUID、实例摘要、规则键、Worker 身份、连续条件起点、触发/恢复时间、触发观测、恢复观测、阈值及窗口。实例摘要是绝对名单路径规范化后的 SHA-256，API 不返回本机路径。界面区分当前和历史实例。

同一实例、规则和 Worker 身份的持续异常复用一个事件；恢复后再次触发产生新 UUID。触发观测保持原值，不每秒覆盖或写库。HTTP 重启时先读取该实例尚未恢复的事件，再启动检测，避免对已持久化的持续异常重复建事件。活动队列事件沿用原阈值和恢复窗口，新事件采用当前配置。

## 配置、建表与权限

沿用阶段 3 数据库环境变量和本机 Bearer 鉴权。管理员在独立监控库执行更新后的 `sql/monitor.sql`，已有实验表不变；给监控账号追加新表权限：

```sql
GRANT SELECT, INSERT, UPDATE ON epoll_monitor.monitor_event TO 'epoll_monitor'@'127.0.0.1';
```

不修改原业务库或原业务账号，不由 HTTP 自行建表或授权。无数据库配置时事件 API 返回 503 `event_store_disabled`，实时监控照常运行。库不可用、缺表或缺权限时检测初始化等待恢复，不绕过历史加载重新制造事件。

| 环境变量 | 默认 | 范围 |
|---|---:|---|
| EPOLL_MONITOR_EVENT_TRIGGER_MS | 3000 | 1～60000 ms |
| EPOLL_MONITOR_EVENT_RECOVERY_MS | 3000 | 1～60000 ms |
| EPOLL_MONITOR_RECEIVE_QUEUE_THRESHOLD | 1024 | 1～2147483647 条 |
| EPOLL_MONITOR_SEND_QUEUE_THRESHOLD | 1024 | 1～2147483647 条 |

非法值启动返回 64；实际检测精度受 500 ms 调度与快照发布频率约束。1024 是可调开发默认值，不是通过生产负载得出的报警阈值。

事件存储复用数据库配置构造函数，使用独立于实验查询的一条数据库连接。检测线程、写库线程与原采集线程分开，SQL 超时 2 秒。内存至多保存 16384 个待写事件、12289 个规则状态；活动事件恢复可合并更新同一待写记录，UUID upsert 支持重试。写库失败保持待写数据并重试，不阻塞实时采集。队列容量不足时暂停转换并重置窗口，API 明示 backpressure；不宣称停顿期间完成检测。初始加载超过活动状态容量时不启动检测。

待写队列只在内存中，监控退出或崩溃时未写入的转换可能丢失，退出时报告剩余数量；已提交记录跨重启保留。第一版要求同一名单只运行一个检测监控进程，不提供多监控副本协调。数据库记录不自动删除，长期归档由管理员管理；并未实现无限历史分页或 WAL。

## API 与页面

`GET /api/v1/events` 返回最近 100 条已持久化事件，按触发时间和 UUID 降序；不接受请求体、查询参数或写方法。继续使用 schema_version、timestamp_unix_ms、code、validity、data_valid、data 封装；成功为 stored，不代表 TCP 健康。

data 包含 events、limit=100、detection。detection 展示当前实例、初始化状态、检测状态、存储状态、待写数量、容量及背压。列表是数据库记录，刚触发尚未提交的事件不出现在列表；该状态摘要采自查询开始时。SQL 故障返回 `event_store_unavailable`，最多 8 个事件查询在途，超限为 `event_store_busy`，不会泄露 SQL 或凭据。

现有网页新增「异常事件」页，展示规则、实例、Worker、状态、触发/恢复时间、观测值、阈值和窗口。停留事件页时每 2 秒刷新，防止请求堆积；错误及断开会清空记录，令牌仍仅在页面内存中。实时指标与事件请求分别处理。

## 验收入口

```bash
make -C monitor http
PYTHONPATH=testscript python3 -m unittest test_event_rules test_http_monitor test_import_experiment test_stage3_lifecycle -v
# 既有测试资源生命周期预检
python3 testscript/test_stage3_live.py --database-only
# 一条真实事件：触发 → MySQL 精确读回 → 恢复 → 退出清理
python3 testscript/test_stage4_live.py --smoke
# 故障、重启、双队列、SQL 权限故障及真实 TCP Worker 暂停/恢复
python3 testscript/test_stage4_live.py
# 同一完整验收结束后保留服务供浏览器；回车后清理
python3 testscript/test_stage4_live.py --hold
```

规则测试使用生产 C++ 状态机和显式单调时间。数据库/API 前半部分使用真实 /proc 身份的受控快照，队列数为故障注入，不冒充真实业务积压；后半部分使用自建真实 TCP 实例，验证 Ping、Worker 暂停导致过期事件、继续后恢复，并核验没有将暂停误报为退出。测试窗口缩短为 500 ms，默认 3000 ms 的边界由纯规则测试覆盖。不生成性能负载，不复测阶段 0/3 性能。

私有 MySQL 使用管理员 SHUTDOWN 并 wait 后再删目录；TCP 由本次私有进程组退出，确认后删临时配置；HTTP 和快照发布线程独立回收。失败保留诊断现场，不批量停止同名服务。

## 本批验收结果

| 检查 | 结果 |
|---|---|
| 构建与静态检查 | Make HTTP C++17 -Wall -Wextra -Werror 编译通过；JS/Python 语法和 git diff --check 通过 |
| 针对性回归 | 24 项通过（14.086 秒）：生产规则状态机、11 项 HTTP、4 项导入器、8 项资源生命周期；含默认 3 秒边界、持续去重、未知中断恢复、重新触发、恢复活动事件、时钟间隔、正常停机抑制和配置拒绝 |
| 最小真实链路 | 私有 MySQL 生命周期预检通过；一条真实事件触发、SQL 精确读回、恢复与清理通过 |
| 完整故障联调 | 接收/发送队列、持续去重、HTTP 重启恢复同一事件、缺失数据不误恢复、撤销写权限后实时采集独立运行、授权恢复后补写、真实进程退出和正常停止抑制通过 |
| 真实 TCP | 自建实例 Ping 通过；暂停一名 Worker 后产生采集事件，未误报进程退出；继续后恢复事件和完整采集 |
| 浏览器 | 事件列表、当前/历史实例、恢复时间、断开清理、零浏览器脚本错误；390 px 页面宽度为 390，令牌无浏览器持久化；修复后表格可聚焦并由右箭头横向滚动 |
| 可访问性 | 桌面 axe WCAG 2A/2AA 零违规、零待检查；窄屏零自动违规，横向滚动区外的 4 处文字对比度标记待人工检查，已结合桌面完整检查、相同样式与截图核对，未宣称全站完整 WCAG 认证 |
| 阶段 3 兼容 | 复用六份既有真实报告，精确读回、对比、HTTP 重启持久化、数据库故障/过载隔离及清理通过，没有重新压测 |

完整阶段 4 故障证据在 `artifacts/stage4-live-eizy_61b/`，含七条事件与真实 TCP 日志；最终页面修复后的截图和最小链路在 `artifacts/stage4-live-5qd1eodh/`。初次沙箱外最小链路为 `artifacts/stage4-live-omyf0ngt/`；阶段 3 兼容联调为 `artifacts/stage3-live-rnegb2u6/`。

首次沙箱内私有 MySQL 初始化等待 90 秒超时，脚本保留句柄和 `/tmp/epoll-monitor-mysql-mkgba4v6`，没有删除仍可能使用的数据。按权限机制转到沙箱外后真实链路通过。后续宿主机核验没有任何 mysqld 使用该目录，归档 init 日志到 `artifacts/stage4-live-bb_iot2t/` 后删除；超时的精确根因未查明，不归因为确定的 MySQL 故障。新脚本曾因从错误模块导入 connect 而在启动服务前失败，已改用既有 test_shutdown.connect，完整实测通过。

最终私有 MySQL 均通过管理员 SHUTDOWN、退出确认后删除数据目录，测试 TCP/HTTP、浏览器和发布线程已退出；临时 TCP 配置已删除。宿主机核验仅剩原有系统 MySQL PID 2319，没有 nginx 或 epoll-monitor。未改动原业务表、数据库授权或本机 nginx.conf，既有未提交修改保留。

未做长期数据库/磁盘故障、填满内存缓冲、多监控副本或崩溃瞬间待写数据耐久性验收。正式监控库初始化、生产部署和正式监控开销评估未执行；未提交或推送。本批按用户确认范围完成功能验收。
