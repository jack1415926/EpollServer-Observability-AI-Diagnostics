# EpollServer 优化与 Agent 集成路线图

> 更新日期：2026-09-15。本文保留优化路线与可选后续；当前事实以 [状态摘要](CURRENT_STATUS_AND_NEXT_STEPS.md) 为准，性能数据只维护在 [性能验证说明](PERFORMANCE_VALIDATION.md)。

新会话的工作树状态、实测证据和下一步命令见 [CURRENT_STATUS_AND_NEXT_STEPS.md](CURRENT_STATUS_AND_NEXT_STEPS.md)。

## 1. 当前定位

当前项目已经具备 Linux C++17 服务端的完整学习链路：Master/Worker、epoll LT、非阻塞 Socket、线程池、自定义二进制协议、MySQL 连接池、Redis Cache-Aside、Qt 客户端和 Python 联调脚本。它适合作为 C++ 后端学习与面试项目，但尚不应描述为生产可用或经过充分性能验证的服务。

### 本轮五项优化进度

以下按本轮约定的执行顺序排列：第三项合并原 P0 的“优雅退出”和“并发关闭”；第五项对应下文 P1 压测与 P2 性能优化，不改变原优先级编号。

| 顺序 | 工作项 | 代码进度 | 验证状态 |
|---|---|---|---|
| 1 | Redis 在 Worker fork 后初始化 | 已修改 | Ubuntu 实机联调通过；4 个 Worker 均观察到独立 Redis TCP 连接 |
| 2 | CRC、完整收包、超时与失败断言 | 已修改 | Linux 离线协议回归 3 项及真实注册、登录、用户查询联调通过 |
| 3 | 优雅退出与发送/关闭并发保护 | 已修改，已提供回归脚本 | Debug 编译、线程池排空和 4 个停机场景通过；慢读及 sanitizer 待验收 |
| 4 | 密码存储与响应认证信息 | 未开始 | 当前仍有明文密码存储和注册响应回传密码，不能视为安全问题已解决 |
| 5 | 可复现压测，再根据数据优化 | 个人项目范围内完成 | Redis-off/on同口径中位数约5.19W/2.79W，均600万请求全部成功；量化每请求同步Redis Lua吞吐成本约46%，外部发压与Release不在当前范围 |

功能与停机验收最初在 6-vCPU VMware Ubuntu 24.04.5 VM 完成，阶段性性能对照随后在 12 vCPU 下完成。临时日志不作为长期证据，复现步骤见 [SHUTDOWN_VALIDATION.md](SHUTDOWN_VALIDATION.md)。

可复现压测已在个人项目范围内收尾；认证边界仍是未完成的 P0 项。

## 2. 优化优先级

### P0：先保证正确性和可验证性

1. **Redis 改为 fork 后初始化（已通过 Linux 实机验收）**：`CLogicSocket::Initialize()` 只调用父类初始化监听资源；`ngx_worker_process_init()` 在启动线程前调用 `InitializeRedis()`，每个 Worker 独立创建 Redis 连接池。真实请求后通过 `ss -ntp` 观察到 4 个 Worker 分别持有不同的 Redis TCP 连接；这证明了本次运行中的独立建连，但不等同于长期稳定性或性能验证。
2. **修复协议测试脚本（已通过 Linux 离线及真实联调）**：两个脚本共用 `testscript/protocol.py`，发送包体 CRC32，完整接收并校验响应 CRC、包长、命令码、业务结果及返回身份。连接和读写默认超时 10 秒，失败退出码为 1，断言在 `python -O` 下仍有效。Linux 离线协议回归 3 项通过；真实 MySQL/Redis 服务上的注册、登录及存在/不存在用户查询通过。Redis 中已直接核对正常 JSON 缓存和 `NULL_USER` 短 TTL，而不是仅凭重复响应推断缓存命中。

   ```bash
   # 离线协议及模拟服务回归，无需 MySQL/Redis
   python -m unittest discover -s testscript -p test_protocol.py -v
   # 真实联调：替换地址和已知的用户 ID，每次注册会新增一条测试数据
   python testscript/test_register_login.py --host 127.0.0.1 --port 8080 --timeout 10
   python testscript/test_get_user_info.py --host 127.0.0.1 --exist-id 1 --missing-id 99999
   ```
3. **实现优雅退出（已通过 Ubuntu 运行验收）**：SIGTERM/SIGQUIT/SIGINT 只设置信号标记，Master 在主循环转发停止信号并回收 Worker；Worker 停止接收，线程池处理完已入队任务，最多再用 5 秒发送剩余响应，然后停止辅助线程、释放连接及 MySQL/Redis。异常 Worker 退出采用整组停止策略，Master 返回 1，不自动重启。线程池排空测试和 4 个停机场景通过；慢读与 ASan/TSan 仍未覆盖。测试步骤见 [SHUTDOWN_VALIDATION.md](SHUTDOWN_VALIDATION.md)。
4. **收紧认证边界（未开始）**：密码改为带盐哈希，响应不再携带密码；公开部署前补充传输加密、会话身份和管理接口鉴权。
5. **处理连接并发关闭（功能与停机验收通过）**：每个 Worker 使用网络状态锁串行化收发、关闭和回收；锁不跨越 MySQL/Redis 调用。业务任务执行期间固定连接对象，关闭后旧响应通过序列号检查丢弃；重复关闭不重复回收，发送使用 `MSG_NOSIGNAL`。RST/并发停机已覆盖，慢读和 TSan 仍待专项验证。

完成标准：Linux 环境可重复构建；功能脚本能自动判定通过/失败；多 Worker、断连和退出场景留下可复现实验记录。

### P1：建立可信的工程验证

- 为包头编解码、CRC 和关键配置解析增加少量确定性测试，不为追求数量引入庞大测试框架。
- 增加 ASan/UBSan 构建；并发路径再使用 TSan 做专项检查。
- 压测工具已支持多进程、延迟抽样、严格 Ping 校验、Linux 内核收发超时和同轮资源采样。最终结果存在 VMware 时段波动，当前只作为本地阶段性基准；详见性能文档。
- 同步 README 与实现状态，性能数据没有实测前不使用“海量并发”“工业级”等结论。

### P2：基于数据优化性能

- 已量化每请求同步 Redis `EVAL` 的成本：QPS 中位数约下降 46%。后续只有在继续优化限流时，再评估本地一级计数、`EVALSHA` 或批量上报。
- 回收队列已在第三项修改中使用迭代器连续删除，但未测量性能收益；定时器删除路径仍有从头遍历，后续根据数据优化，只有测得瓶颈后再增加反向索引等额外结构。
- 根据压测调整当前 `4 Worker × 120` 业务线程及数据库连接池大小，不把更大的线程数默认视为更高性能。

当前压测工具、最终对照与边界见 [PERFORMANCE_VALIDATION.md](PERFORMANCE_VALIDATION.md)。

## 3. 推荐的 Agent 方向

最适合本项目的第一步不是让 Agent 控制服务器，而是先提供独立的只读接口。当前已增加 `epoll_mcp/` stdio sidecar，不进入 epoll I/O 热路径；完整运维与故障诊断 Agent 仍是后续计划。

```text
用户 / Qt 管理页
        |
        v
外部 Agent
        |
        v
epoll_mcp（Python/stdio，已实现）
  |- epoll_ping_server：验证服务和二进制 Ping 协议
  |- epoll_search_docs：检索白名单项目文档
  `- epoll_tail_log：读取固定 error.log 尾部
        |
        v
EpollServer（C++ 数据面，不调用大模型）
```

当前 MCP MVP：

```text
epoll_mcp/server.py           # 三个只读工具与 stdio 入口
testscript/test_mcp_tools.py  # 离线工具回归
requirements-mcp.txt          # 固定 MCP SDK 版本
```

验证状态（2026-09-14）：4 项 MCP 与 3 项协议离线测试在 Linux 通过；stdio MCP Client 成功发现三个工具，并完成结构化文档检索、固定日志读取和真实 C++ 服务 Ping。服务停止后 Ping 返回结构化 `connection_failed`，sidecar 正常退出。CRC 错误、错误命令、拒绝连接和超时仍由离线临时协议服务器覆盖。

当前 MCP 不读取任意路径、不执行 Shell，也不修改配置、重启服务或操作数据库。只有当只读流程稳定后，再考虑新增带鉴权的 `_CMD_SERVER_STATS` 和独立诊断 Agent；跨机器 HTTP 还需要认证、TLS 与来源限制。

## 4. 与 KBrag V6 的关联方式

关联对象是仓库外的 KBrag V6 项目；本文只记录方法级衔接，不依赖机器特定路径。

当前已验证，V6 使用 FastAPI 薄入口、手写有界 ReAct 循环和单一 `search_manual` 工具；技术问题经过产品路由、混合检索、RRF、rerank 后返回 parent section 证据。其 `agent.py`、提示词、图片格式和 `ProductRouter` 与产品手册客服领域紧密耦合，因此不宜直接整体复制到 EpollServer。

推荐分三步建立联系：

1. **复用方法，不复制业务代码**：沿用“路由 → 检索工具 → 观察 → 有界收束”和“答案必须带证据”的设计，把 `search_manual` 换成 `search_epoll_docs`。
2. **增加协议适配器**：P0 安全问题解决后，由 `agent_assistant/tools/epoll_client.py` 统一实现包头、CRC、超时和响应解析，调用只读状态命令。
3. **形成跨项目演示**：V6 负责 Agent/RAG 与工具编排，EpollServer 负责高并发连接、账号数据和运行状态。演示问题可以是“登录请求为什么无响应”“当前 Worker/连接数是否异常”“这条 Redis 日志如何排查”。

若以后两个项目确实出现两种以上稳定的共用工具，再抽取通用 Agent 核心；在此之前保持 API/协议边界，避免为了“关联”制造共享代码包。

## 5. 建议实施顺序

只读 stdio MCP MVP 已作为独立支线提前落地。主线后续顺序保持为：

```text
密码存储与响应认证信息
  -> 受限状态查询工具
  -> 完整只读诊断 Agent
  -> V6 跨项目演示
```
