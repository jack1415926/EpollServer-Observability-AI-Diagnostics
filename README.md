# EpollServer Observability & AI Diagnostics

基于既有 Linux C++ TCP 服务，逐步开发运行指标采集、独立 HTTP 监控与 Python RAG 辅助故障诊断。

**阶段 0～3 已按确认范围完成功能验收**。当前可通过本机 Drogon 页面观察真实 TCP 实例与 Worker，导入压测结果和资源摘要，使用独立 MySQL 监控表查询历史并比较两次实验。50 项合并回归通过；并发 10 / 20 各三轮正式共 18 万次 Ping 零失败，落库、重启持久化、数据库故障/过载隔离和桌面/窄屏浏览器验收通过。详情见 [阶段 3 Web 与性能实验](docs/STAGE_3_WEB_EXPERIMENTS.md)。

阶段 4 已实现三类基础异常检测：采集缺失/过期、Worker 进程异常、收发队列持续积压；支持 MySQL 事件持久化、持续去重、自动恢复、查询 API 与网页列表。默认检测/恢复各 3 秒、队列阈值 1024 条，均可配置。规则、部署权限、验收及边界见 [阶段 4 事件](docs/STAGE_4_EVENTS.md)。

阶段 1 的 Worker 快照、Master 名单与 C++ 汇总支持身份核验、过期、部分失败和旧实例隔离，采样默认关闭。阶段 2 提供本机绑定、Bearer 鉴权、限流、后台采集与缓存失效。基础指标完整不代表 TCP 业务健康，主动探测仍未实现；累计计数和数据库内部指标尚未支持。原负载口径的正式监控开销验收留待后续。

T7第一批已增加Worker进程CPU使用率和近似RSS常驻内存，由独立监控进程读取`/proc`，在业务快照过期时仍可独立观测。CPU首次采样为null，100%表示一个逻辑CPU；页面与原只读诊断工具接通，未增加资源告警。契约与验证范围见[T7资源观测](docs/T7_WORKER_RESOURCES.md)。

阶段 5 已接通独立 Python 诊断服务、DeepSeek Flash、Qwen 混合检索与重排、七种只读工具、异步任务、MySQL 报告持久化和网页。真实 Worker 暂停场景及 AI 故障隔离已通过；运行、权限、证据与验收边界见 [阶段 5 诊断](docs/STAGE_5_DIAGNOSIS.md)。

后续 T11/T12 已交付按问题补充当前代码契约、普通观测与故障分析分开回答、JSON 原值/数量和摘录定位、来源范围检查、任务级检索/模型元数据及拒绝报告持久化。程序检查不证明任意自然语言推断正确，报告仍需语义复核；本批对照、保留的拒绝/部分支持结果和真实 HTTP/MySQL 验收见 [证据策略与支持校验](docs/DIAGNOSIS_EVIDENCE_POLICY.md)。

根据用户回传的 Ubuntu 验收输出，200 并发、6 个客户端进程、Redis Lua 开启的三轮 Ping 测试累计 600 万请求零失败，QPS 中位数为 32,203.656，抽样 P95 的三轮中位数为 9.411 ms。环境、完整口径和证据限制见 [本次 Linux 验收记录](docs/BASELINE_RESULTS_2026-10-23.md)。该结果是同机 Debug Ping 基线，不代表生产业务容量。

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
| `server/`、`sql/` | 从固定提交原样导入的 TCP 服务与数据库初始化材料 |
| `testscript/` | 原协议、集成、停机与压测脚本 |
| `epoll_mcp/` | 原有只读 stdio 工具，不代表新诊断服务已完成 |
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

1. 当前进度见 [本次 Linux 验收记录](docs/BASELINE_RESULTS_2026-10-23.md)；复现步骤见 [阶段 0 验收说明](docs/STAGE_0_BASELINE.md)，已有通过项无需重复执行。
2. 设计边界及对规划书的调整见 [需求与实施决策](docs/DECISIONS.md)。
3. 上游地址、提交和导入范围见 [源码来源记录](docs/SOURCE_BASELINE.json)。
4. [原始规划书](docs/PROJECT_PLAN_V1.0_ORIGINAL.md)原文保留；发生冲突时以已确认决策为准。
5. 新增 Worker 快照的字段、故障语义和合并验收命令见 [阶段 1 第一批](docs/STAGE_1_WORKER_SNAPSHOT.md)。
6. Master 名单字段、生命周期与最新合并验收见 [阶段 1 第二批](docs/STAGE_1_MASTER_MEMBERSHIP.md)。
7. 当前完成标准、C++ CLI 构建使用、完整功能验收及小规模开销记录见 [阶段 1 收尾](docs/STAGE_1_COMPLETION.md)。
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
