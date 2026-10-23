# EpollServer Observability & AI Diagnostics

基于既有 Linux C++ TCP 服务，逐步开发运行指标采集、独立 HTTP 监控与 Python RAG 辅助故障诊断。

**阶段 0 和阶段 1（用户确认的基础指标范围）已完成验收**。阶段 1 包含 Worker 快照、Master 名单、C++ 读取汇总与 CLI，支持身份核验、过期、部分失败和旧实例隔离；2026-10-24 通过 Linux 编译、30 项回归、5 场景端到端联调及 4 场景停机回归。采样默认关闭。小规模开关对照正式 36 万次 Ping 零失败，原负载口径的正式开销验收留待后续；CPU/内存、累计计数和数据库内部指标尚未实现。Drogon、Web 和 AI 尚未开始。

根据用户回传的 Ubuntu 验收输出，200 并发、6 个客户端进程、Redis Lua 开启的三轮 Ping 测试累计 600 万请求零失败，QPS 中位数为 32,203.656，抽样 P95 的三轮中位数为 9.411 ms。环境、完整口径和证据限制见 [本次 Linux 验收记录](docs/BASELINE_RESULTS_2026-10-23.md)。该结果是同机 Debug Ping 基线，不代表生产业务容量。

## 已确认的边界

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
| `docs/upstream/` | 原服务器文档快照，历史结果不代表本项目已复测 |
| `sources/` | 两个完整参考克隆，本地保留，不纳入新仓库 |
| `docs/` | 原始规划、本次需求决策与阶段验收说明 |

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

原服务依赖 Linux、G++ C++17、Make、pthread、MySQL client、hiredis 和 redis-plus-plus。构建沿用 Make：

```bash
make -C server
# 配置完成且 MySQL、Redis 可用后，从配置文件所在目录启动：
cd server
./nginx
```

`nginx.conf` 按当前工作目录加载。现有用户认证仍有明文密码和注册响应回传密码的问题；本阶段仅用于隔离开发环境，公开部署前另行修复。

项目远端为 [jack1415926/EpollServer-Observability-AI-Diagnostics](https://github.com/jack1415926/EpollServer-Observability-AI-Diagnostics)。原参考克隆的 `origin` 仍指向原仓库，后续成果在当前根仓库维护。克隆新项目即可取得当前阶段所需源码；两个参考仓库不是构建依赖，KBrag 诊断模块尚未导入。
