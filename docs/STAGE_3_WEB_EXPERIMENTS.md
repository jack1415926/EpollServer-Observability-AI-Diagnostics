# 阶段 3：Web 监控与性能实验

2026-10-24，完成功能验收：网页观察真实实例，保存、查询和比较实验，并复现并发 10 / 20 两组各三轮 Ping 实验。事件与 AI 不在本阶段范围。

## 页面与服务

启动现有 `epoll-monitor` 后访问 `http://127.0.0.1:8080/`：

- **运行总览**：当前连接、有效 Worker、收发队列、Master 与采集状态。
- **Worker 状态**：进程身份、采集状态、连接/队列和采集时的样本年龄。
- **性能实验**：最近 100 条历史记录、实验详情、JSON 导入、基准与候选对比。

原生 HTML/CSS/JavaScript 无 npm 或前端框架运行依赖。构建时 `monitor/web/embed.py` 将三个固定资源嵌入 C++ 二进制，不按 URL 读取任意文件。`/`、`/app.js`、`/style.css` 可匿名加载，页面本身没有运行数据或凭据；所有 API 继续要求 Bearer 令牌。

令牌仅存当前页面内存，输入后清空密码框，不写 Cookie、localStorage 或 sessionStorage。断开清理指标、列表、详情和对比。页面每 2 秒刷新实时指标，防止刷新堆积；错误时清除实时数值，缺失或不完整总量显示「—」。历史记录与实时监控分别处理，数据库失败不阻断实时采集；`tcp_health=not_probed` 语义不变。

资源带 no-store、nosniff 和限制脚本/连接来源的 CSP。数据通过文本节点渲染，导入名称中的 HTML 不执行。表格在窄屏容器内横向滚动，页面本身无横向溢出。

## 数据库初始化与启动

沿用 [阶段 2 构建](STAGE_2_HTTP_MONITOR.md)，执行 `make -C monitor http`。服务不读取 `server/nginx.conf` 中的数据库凭据，不自行建表或授权。管理员初始化独立监控数据库：

```bash
sudo mysql -e 'CREATE DATABASE IF NOT EXISTS epoll_monitor CHARACTER SET utf8mb4;'
sudo mysql epoll_monitor < sql/monitor.sql
sudo mysql
```

在管理员会话中设置自选强密码，下面的密码是占位符：

```sql
CREATE USER 'epoll_monitor'@'127.0.0.1' IDENTIFIED BY 'REPLACE_WITH_YOUR_PASSWORD';
GRANT SELECT, INSERT ON epoll_monitor.monitor_experiment TO 'epoll_monitor'@'127.0.0.1';
```

独立表 `monitor_experiment` 保存 UUID 主键、JSON 报告与带索引的创建时间，不修改 TCP 业务表。HTTP 账号不需要 CREATE、UPDATE 或 DELETE。

```bash
export EPOLL_MONITOR_DB_HOST=127.0.0.1
export EPOLL_MONITOR_DB_PORT=3306
export EPOLL_MONITOR_DB_NAME=epoll_monitor
export EPOLL_MONITOR_DB_USER=epoll_monitor
read -rsp 'Monitoring DB password: ' EPOLL_MONITOR_DB_PASSWORD
export EPOLL_MONITOR_DB_PASSWORD
read -rsp 'Monitor token: ' EPOLL_MONITOR_TOKEN
export EPOLL_MONITOR_TOKEN
./monitor/build/epoll-monitor /absolute/metrics/master-BOOT-PID-TICKS.json 8080
```

数据库名未配置或为空时实时监控照常工作，实验 API 返回 `experiment_store_disabled`。Host/Port/User 默认 127.0.0.1 / 3306 / epoll_monitor。数据库名、用户名和主机支持 1～128 个 ASCII 字母、数字或 `_.:-`；密码支持空格、引号与反斜杠，不接受控制字符，最多 256 字节。凭据不返回客户端。

使用本地解包的 MariaDB 库连接 MySQL 8.4 时，在启动前设置认证插件目录；系统已安装插件时无需设置：

```bash
export MARIADB_PLUGIN_DIR="$PWD/.deps/usr/lib/$(g++ -dumpmachine)/libmariadb3/plugin"
```

依据：[MariaDB Connector/C 官方 plugin-dir 说明](https://mariadb.com/docs/connectors/mariadb-connector-c/configuring-mariadb-connectorc-with-option-files)。

## 生成与导入实验

从项目根目录执行，需要已有开发 MySQL / Redis 供隔离 TCP 实例启动，不创建业务用户：

```bash
python3 testscript/run_monitor_experiments.py --binary ./server/nginx --output ./artifacts
```

脚本启动独立 2 Worker × 4 业务线程 TCP 实例，启用快照和 Redis Lua 高阈值限流，固定服务端前两个可用 CPU、客户端随后两个 CPU。预热 5000 次，然后按并发 10、20、20、10、10、20 顺序执行，每个客户端 2000 次 Ping，2 个客户端进程，strict/into/kernel-timeout，超时 5 秒，延迟全量采集；每 0.2 秒采样进程及系统资源。每轮输出：

| 文件 | 内容 |
|---|---|
| `round-N-result.json` | 原压测结果，新增 load.timeout_sec 和 result.latency_ms.mean |
| `round-N-resources.json` | 原始资源采样，保留于本地 |
| `round-N-context.json` | 环境标识、二进制/客户端脚本 SHA-256、CPU 绑定、快照与限流参数、实际命令 |
| `round-N-experiment.json` | 可在网页导入的报告，含上述结果、资源摘要、原始文件哈希，不含配置、日志或原始采样数组 |

已有结果可使用对应的脱敏 context 导入：

```bash
python3 testscript/import_experiment.py --name '并发 10，第一轮' \
  --result /path/round-1-result.json --resources /path/round-1-resources.json \
  --context /path/round-1-context.json --out /path/experiment.json
# 增加 --url http://127.0.0.1:8080 可直接导入，令牌来自环境。
```

导入器限制单个输入文件 8 MiB，检查结果与资源 label 一致、拒绝重复 JSON 键，报告最多 64 KiB。服务器验证固定字段集合、类型、计数与请求数关系、成功率、延迟分位数/样本数、哈希长度、嵌套深度等，不通过 HTTP 暴露原始文件。

兼容原脚本 7～40 位十六进制提交号，旧结果没有均值时保留缺失，不从 P50/P95 推算。mean 是**已采集延迟样本均值**，全量模式才覆盖所有成功请求。每次导入新增 UUID，重复导入会产生副本；SQL 超时不保证取消已执行的写入，重试前先查询记录。

## API 与对比语义

沿用时间戳、code、validity、data_valid、data 封装。历史成功为 `validity=stored`、`data_valid=true`，不表示当前实例健康。

| 路径与方法 | 行为 |
|---|---|
| GET `/api/v1/experiments` | 最近 100 条摘要，按创建时间/UUID 降序，limit=100，不接受分页参数 |
| POST `/api/v1/experiments` | 导入报告，201 返回 UUID |
| GET `/api/v1/experiments/{id}` | 完整报告与创建时间，不存在为 404 |
| POST `/api/v1/experiments/compare` | JSON baseline/candidate 两个 UUID；返回条件差异、QPS、P95、延迟样本均值和未成功占比的基准/候选/差值/百分比变化 |

百分比变化为 `(候选-基准)/基准*100`；零基准或数据缺失为 null，不返回无穷大或补零。未成功占比包含 failed 和 not_attempted。条件检查覆盖环境、OS/Python、CPU/内存、版本/工作区、目标主机、服务端配置、负载/采样、二进制/客户端脚本哈希、CPU 绑定、快照/限流。`same_conditions` 只表示**已记录字段相同**，不证明环境完全相同；客户端哈希针对压测脚本，不涵盖全部依赖。结果只陈述观测差异，不作因果判断。

SQL 使用绑定参数和异步接口，独立一个数据库连接/线程，最多 8 个待处理请求，超额返回 experiment_store_busy；SQL 等待超时为 2 秒，故障返回 experiment_store_unavailable。数据库异常正文、连接串和密码不返回客户端。单个详细报告最多 64 KiB；不提供远程执行压测/Shell、删除记录或修改 TCP 配置入口。

解析器总请求体上限为 128 KiB，全部在内存中；应用限制实验路由 64 KiB、原实时路由 1 KiB。超限 413，解析器提前拒绝可能无 JSON。原鉴权、每秒 100 请求/突发 100、64 连接等限制继续生效。

## 验收

```bash
make -C server
make -C monitor
make -C monitor http
PYTHONPATH=testscript python3 -m unittest test_protocol test_tcp_stress \
  test_performance_sampler test_worker_snapshot test_metrics_collector \
  test_service_fixes test_http_monitor test_import_experiment -v
python3 testscript/test_stage3_live.py
git diff --check
```

真实联调需要 mysqld、mysql CLI 和 TCP 原有数据库/Redis。测试新建只监听回环的私有 MySQL 及仅 SELECT/INSERT 的账号，不向业务账号追加权限。AppArmor 允许 `/tmp` 中本用户文件，因此测试数据目录位于 `/tmp`；通过私有管理员执行 SHUTDOWN，确认退出后才复制诊断日志并删除目录。退出失败保留目录，禁止提前删除。

| 检查 | 结果 |
|---|---|
| 编译/语法 | C++17 -Wall -Wextra -Werror、JS/Python 语法及 diff 检查通过 |
| 合并回归 | 50 项通过，26.996 秒：原 37、HTTP 10、导入器 3；最终退出码整数边界另补跑相关用例 |
| MySQL | 六条正式报告导入并原样读回；对比、条件差异、零基准/不存在 ID、HTTP 重启后记录保留通过 |
| 故障隔离 | 错误数据库端口有界返回 503；16 个并发历史查询触发 8 请求上限，实时指标继续完整并快速响应 |
| 浏览器 | 总览/Worker、列表/详情/资源关联、真实对比、文件导入、HTML 名称纯文本渲染、断开清理、错误令牌、断网失效/恢复、刷新丢弃令牌通过 |
| 布局/可访问性 | 390 px 无整体横向溢出；总览与实验页 axe WCAG 2A/2AA 均零违规，无脚本错误 |

正式负载（全量延迟采集）：

| 并发 | 轮数 | 成功 / 失败 | QPS 三轮中位数 | P95 ms 三轮中位数 |
|---|---:|---:|---:|---:|
| 10 | 3 | 60000 / 0 | 16846.716 | 0.917 |
| 20 | 3 | 120000 / 0 | 23458.368 | 1.306 |

正式 18 万次，另预热 5000 次，均零失败。负载不同，仅作为本批可复现演示；不与阶段 0 的 200 并发、阶段 1 抽样延迟或其他环境直接比较，不据此宣称性能提升或正式监控开销达标。

负载证据 `artifacts/stage3-live-dvpeok8b/experiments/`，临时配置已删除。最终落库/故障/浏览器证据 `artifacts/stage3-live-9hsjbsde/`，含真实 comparison JSON、截图及摘要。浏览器导入额外添加一条真实报告的测试副本，仅存在私有数据库中。测试结束已确认私有 MySQL、TCP/HTTP 和浏览器清理成功，仅剩原有系统 MySQL；正式监控库尚未初始化。阶段 3 基于 f03f3f3 的未提交工作树验收，产物和依赖不纳入 Git。

首次测试清理在信号被拒绝后由 TemporaryDirectory 提前删除数据目录，导致存活的 MySQL 写错误日志耗尽临时目录配额。这是本批测试脚本问题；环境恢复后确认旧进程不存在、空间恢复。脚本已改为 SHUTDOWN、等待退出、最后删除，后续真实联调确认清理成功；未修改系统 AppArmor 或原有 MySQL。

未执行生产部署、远程 TLS、长时间数据库/磁盘故障、全服务 sanitizer 或原 200 并发的正式监控开销验收。历史 API 仅查最近 100 条，长期归档由管理员管理。未扩展事件、CPU/内存实时指标或 AI。
