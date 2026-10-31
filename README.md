# EpollServer Observability & AI Diagnostics

基于既有 Linux C++ TCP 服务，逐步开发运行指标采集、独立 HTTP 监控与 Python RAG 辅助故障诊断。

## 当前状态

项目功能链路已贯通：C++ TCP采集 → 独立Drogon监控/Web → 事件与实验持久化 → Python只读诊断。**当前进度、下一步和保留待办统一见[当前状态与待办](docs/CURRENT_STATUS.md)**，不按历史阶段文档的“下一步”或T编号判断进度。

| 能力 | 交付范围与边界 |
|---|---|
| 基础采集与HTTP | Worker/Master身份、连接与队列、过期/部分失败、鉴权/限流/缓存；阶段0～3按确认范围完成验收 |
| Web与实验 | 实时总览/Worker、真实实验导入/详情/比较、独立MySQL持久化 |
| 事件 | 采集异常、Worker异常、队列积压三类规则，去重、恢复、查询与页面 |
| 资源观测（T7首批） | CPU窗口使用率、近似RSS，保留主线程状态及累计CPU兼容字段；线程/请求/DB等待等未实现 |
| 只读诊断 | 路由、混合检索/重排、八种只读工具、异步任务、报告持久化与页面；不执行Shell或自动修复 |
| 案例与实验输入 | T6案例初版、T8显式报告目录及UUID实验详情已交付；扩展案例和相似实验查询另列待办 |
| 诊断质量 | T11/T12已交付局部支持校验与复现保护，仍有语义/生成限制；已决定停止继续打磨，不作为下一步前置条件 |

2026-10-30[正式监控开销测量](docs/MONITOR_OVERHEAD.md)已完成：三条件各三轮，1800万次正式Ping及90万次预热零失败。完整监控相对关闭组QPS中位数下降**13.70%**、抽样P95上升**7.47%**，本批吞吐目标未通过。10月31日[分项探索](docs/MONITOR_OVERHEAD_DIAGNOSIS.md)发现逐Worker连接负载未控制；后续[均衡压测尝试](docs/BALANCED_WORKER_BENCHMARK.md)固定各Worker50连接，四轮800万次测量零失败，full的QPS中位数下降约1.97%、P95上升约1.24%。均衡尝试采用新的预热/计时口径且每组两轮，独立保存。下一步已确认采用按负载计算的可配置小范围容差，固定均分保留为基准；范围模式待实现，再统一新方法做正式确认。

功能完成不等于正式部署或诊断质量达标。基础采集完整不代表TCP业务健康，主动探测仍未实现；缺失值保持null。正式监控库初始化与部署尚未完成。

## 已确认的边界

开发或 Code Review 中发现的问题，应简要记录现象、原因、修复方式及验证结果；未解决的注明状态。统一维护在 [开发问题与修复记录](docs/DEVELOPMENT_ISSUES.md)。

后续开发先阅读 [AGENTS.md](AGENTS.md) 和 [开发流程与经验](docs/DEVELOPMENT_WORKFLOW.md)，执行依赖/真实样例验证、测试资源退出检查和分层验收，落实阶段 3 复盘中的改进要求。

- 独立新仓库，C++ 为主线，兼顾 Python 与 Agent/RAG。
- 保留自定义 TCP 协议和现有业务；HTTP 只承担监控与诊断入口。
- 第一版保留原故障策略：Worker 意外退出会触发整个 TCP 服务退出，不做自动补起。
- 使用现有 Ubuntu 虚拟机；阶段 0 由项目所有者执行并回传结果，本批由助手在该 Linux 工作区直接执行验收。
- 每批交付一个小目标，验收后再进入下一批。

## 目录

| 路径 | 内容 |
|---|---|
| `server/`、`sql/` | 基于固定上游版本继续开发的TCP服务及数据库初始化材料 |
| `testscript/` | 原协议、集成、停机与压测脚本 |
| `epoll_mcp/` | 保留的旧只读stdio入口；独立新诊断服务位于diagnosis/ |
| `monitor/` | C++ 快照读取器、CLI 及独立 Drogon HTTP 服务 |
| `monitor/web/`、`sql/monitor.sql` | 编译嵌入的监控页面与独立实验记录表 |
| `docs/upstream/` | 原服务器文档快照，历史结果不代表本项目已复测 |
| `sources/` | 两个完整参考克隆，本地保留，不纳入新仓库 |
| `docs/` | 原始规划、本次需求决策与阶段验收说明 |
| `diagnosis/`、`requirements-diagnosis.txt` | 独立 Python RAG/Agent、任务服务与依赖 |

## 从哪里开始

在 Ubuntu 中拉取当前项目：

```bash
mkdir -p ~/projects
cd ~/projects
git clone https://github.com/jack1415926/EpollServer-Observability-AI-Diagnostics.git epoll-observability
cd epoll-observability
```

后续在工作目录没有本地修改时更新：`git pull --ff-only`。

1. 先看[当前状态与待办](docs/CURRENT_STATUS.md)。历史TCP基线见[阶段0结果](docs/BASELINE_RESULTS_2026-10-23.md)，复现入口见[阶段0说明](docs/STAGE_0_BASELINE.md)；历史证据不代表当前代码重新实测。
2. 设计边界及对规划书的调整见 [需求与实施决策](docs/DECISIONS.md)。
3. 上游地址、提交和导入范围见 [源码来源记录](docs/SOURCE_BASELINE.json)。
4. [原始规划书](docs/PROJECT_PLAN_V1.0_ORIGINAL.md)原文保留；发生冲突时以已确认决策为准。
5. 新增 Worker 快照的字段、故障语义和合并验收命令见 [阶段 1 第一批](docs/STAGE_1_WORKER_SNAPSHOT.md)。
6. Master 名单字段、生命周期与最新合并验收见 [阶段 1 第二批](docs/STAGE_1_MASTER_MEMBERSHIP.md)。
7. 阶段1完成标准、C++ CLI构建使用、当批功能验收及小规模开销记录见 [阶段 1 收尾](docs/STAGE_1_COMPLETION.md)。
8. HTTP 构建启动、四接口契约、鉴权与故障隔离验收见 [阶段 2 HTTP 监控](docs/STAGE_2_HTTP_MONITOR.md)。
9. 网页入口、数据库初始化、实验生成/导入/对比与验收见 [阶段 3 Web 与性能实验](docs/STAGE_3_WEB_EXPERIMENTS.md)。
10. 异常规则、事件表权限、配置及故障验收见 [阶段 4 事件](docs/STAGE_4_EVENTS.md)。
11. 模型配置、知识索引、异步任务和诊断报告见 [阶段 5 诊断](docs/STAGE_5_DIAGNOSIS.md)。

原服务依赖 Linux、G++ C++17、Make、pthread、MySQL client、hiredis 和 redis-plus-plus。构建沿用 Make：

```bash
make -C server
# 配置完成且 MySQL、Redis 可用后，从配置文件所在目录启动：
cd server
./nginx
```

`nginx.conf` 按当前工作目录加载。现有用户认证仍有明文密码和注册响应回传密码的问题；本阶段仅用于隔离开发环境，公开部署前另行修复。

项目远端为 [jack1415926/EpollServer-Observability-AI-Diagnostics](https://github.com/jack1415926/EpollServer-Observability-AI-Diagnostics)。原参考克隆的 `origin` 仍指向原仓库，后续成果在当前根仓库维护。克隆新项目即可取得当前阶段所需源码；参考仓库不是构建依赖；阶段 5 按固定 KBrag-V6 的检索和 Agent 方法建立独立诊断模块，未导入其产品问答业务。

主分支整合保留了10月25日的进程原始观测、知识目录和评测资料；当前默认采用受审阅的分层语料，旧知识目录通过`--corpus catalog`显式使用。整合选择与复测边界见[main合并记录](docs/MAIN_MERGE_2026-10-29.md)。
