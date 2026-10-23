# Redis 连接 fork 共享问题 vs epoll 惊群问题

## 问题一：epoll 惊群（效果待验证）

### 现象

客户端发起 TCP 连接请求到达端口 8080 时，所有 Worker 进程的 epoll 同时被唤醒，但只有一个能 `accept()` 成功，其余白跑一趟。

```
客户端 TCP 连接请求
        │
        ▼
  端口 8080（监听 socket）
   ├── Worker-0 epoll 被唤醒 ──┐
   ├── Worker-1 epoll 被唤醒 ──┤  只有 1 个 accept() 成功
   ├── Worker-2 epoll 被唤醒 ──┤  另外 3 个空跑一趟
   └── Worker-3 epoll 被唤醒 ──┘
```

### 本质

多个进程**被动等待**在同一个监听 socket 上，谁来处理新连接的问题。

### 解决方案

`SO_REUSEPORT`：内核在协议栈层面直接把新连接**分发给指定 Worker**，其他 Worker 根本不会被唤醒。

```c
int reuseport = 1;
setsockopt(isock, SOL_SOCKET, SO_REUSEPORT, &reuseport, sizeof(int));
```

代码中已设置 `SO_REUSEPORT`，但监听 socket 在 Master 中创建后由 Worker 继承；本轮没有验证各 Worker 的唤醒行为，因此不能仅据此标记“惊群已解决”。上文为机制示意，不作为项目运行结论。

---

## 问题二：Redis 连接 fork 后共享（已修复并完成运行验收）

### 现象

历史风险示意：如果 Master 在 fork 前已经建立 Redis TCP 连接，Worker 会继承相同 fd。旧代码在 fork 前创建 Redis 对象；是否已实际建连还取决于库的连接时机，下图不是本轮观测记录。

```
fork 之前：
  Master 进程
    └── fd=7 ──── TCP 连接 ──── Redis Server

fork 之后：
  Master 进程
    └── fd=7 ──┐
  Worker-0     │
    └── fd=7 ──┤
  Worker-1     │        共享同一条 TCP 连接
    └── fd=7 ──┼──────── Redis Server
  Worker-2     │
    └── fd=7 ──┤
  Worker-3     │
    └── fd=7 ──┘
```

### 本质

多个进程**主动建立的连接被 fork 继承**，导致 fd 被多处共用。

### 危害

Redis 协议是请求-响应模型。两个 Worker 几乎同时通过同一个 fd 发送请求时：

- Worker-0 发 `GET user:info:1`
- Worker-1 发 `GET user:info:2`
- 响应回来时，谁都可能读到对方的回复，造成**数据错乱**

### 为什么 `SO_REUSEPORT` 救不了

`SO_REUSEPORT` 是 `bind()` + `listen()` 的监听 socket 选项，只对被动接收连接的场景有效。Redis 连接是服务端作为客户端主动 `connect()` 出去的，`SO_REUSEPORT` 完全管不到。

### 解决方案

Redis 对象由 fork 后的 `ngx_worker_process_init()` 调用 `InitializeRedis()` 创建，且在启动线程之前完成。2026-09-14 的真实请求联调中，4 个 Worker 均观察到各自独立的 Redis TCP 连接；这验证了本次运行中的独立建连，但不替代长期稳定性测试。

```
ngx_worker_process_init()
 ├─ g_socket.InitializeRedis()
 ├─ CMysqlConnPool::Init()
 ├─ g_threadpool.Create()
 ├─ g_socket.Initialize_subproc()
 ├─ g_socket.ngx_epoll_init()
 └─ 仅事件主线程解除信号屏蔽
```

---

## 对比总结

| | epoll 惊群 | Redis fd 共享 |
|---|---|---|
| **触发条件** | 新客户端连接到达监听端口 | fork 后子进程继承父进程 fd |
| **方向** | 被动等待连接 | 主动建立连接后被继承 |
| **涉及 socket 类型** | 监听 socket (`listen`/`accept`) | 客户端 socket (`connect`) |
| **`SO_REUSEPORT` 有效** | ✅ 有效，内核分发 | ❌ 无效，不适用于客户端连接 |
| **修复方式** | `setsockopt(SO_REUSEPORT)` | fork 之后各自重新 `connect` |
| **本项目状态** | 唤醒行为未验证 | 已完成 4 Worker 独立连接验收 |

当前进度及验收边界见 [优化路线图](OPTIMIZATION_AND_AGENT_ROADMAP.md)。
