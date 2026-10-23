# 代码评审报告 — Learn_EpollServer

> 历史评审快照。下文的代码位置、风险描述和性能估算对应当时版本，不代表当前实现或实测性能。
>
> 进度更新（2026-09-15）：问题 1 的 Redis 初始化、问题 2 的发送/关闭保护、问题 3 的退出路径和问题 4 的回收队列遍历已调整；Linux 功能、并发停机和阶段性压测已完成。慢读、sanitizer 及其他历史条目未逐项复验。当前状态以 [状态摘要](CURRENT_STATUS_AND_NEXT_STEPS.md) 为准。

**日期：** 2026-07-06
**范围：** 全部源代码（23 个 .cxx 文件、15 个 .h 文件、构建系统）
**方法：** 8 个独立发现者角度 + 独立验证，去重后保留 10 项

---

## 🔴 严重 — 会导致数据损坏或崩溃

### 1. Redis 连接在 fork 前创建，所有 Worker 共享 TCP socket 导致协议损坏

**文件:** `server/logic/ngx_c_slogic.cxx:145`
**类别:** 并发安全性

`CLogicSocket::Initialize()` 在 Master 进程中调用 `new sw::redis::Redis(...)` 创建到 `127.0.0.1:6379` 的 TCP 连接池（`pool_opts.size = 10`）。随后 `ngx_master_process_cycle()` → `fork()` 产生 N 个 Worker 进程。**所有 Worker 继承相同的 TCP socket fd，指向同一个内核 socket 缓冲区。**

当 Worker A 执行 Lua 限流脚本 (`threadRecvProcFunc():220`)，同时 Worker B 执行缓存查询 (`_HandleGetUserInfo():442`) 时，两者通过同一个 fd 写入 Redis wire-protocol 字节流。Redis 服务端收到交错的字节（如 `"INCGETR key\r\n"`），返回协议错误或串号响应。

**触发条件：** `WorkerProcesses >= 2`（当前配置为 4）且 Redis 启用（`m_pRedis != nullptr`）。

**失效模式：** Redis Lua 限流抛异常被 catch 块吞掉（降级放行），Cache-Aside 读缓存抛异常回退 MySQL。整套 Redis 防御体系静默失效，无任何告警。

**修复方向：** 将 `m_pRedis` 的创建从 `CLogicSocket::Initialize()`（fork 前，`nginx.cxx:98`）移至 `ngx_worker_process_init()`（fork 后，`ngx_process_cycle.cxx:170`），与 MySQL 连接池的处理方式一致（`ngx_process_cycle.cxx:196-217`）。

---

### 2. fd 竞争：`send()` 与 `close()` 无锁保护

**文件:** `server/net/ngx_c_socket.cxx:738` / `server/net/ngx_c_socket.cxx:389`
**类别:** 并发安全性

`ServerSendQueueThread` 在仅持有 `m_sendMessageQueueMutex` 的情况下调用 `send(p_Conn->fd, ...)`（第 738 行）。同时，epoll 线程中的 `recvproc()` 或定时器线程中的 `procPingTimeOutChecking()` 可通过 `zdClosesocketProc()` 执行 `close(p_Conn->fd)`（第 389 行）。

两个操作之间没有共享的互斥锁—`m_sendMessageQueueMutex`、`m_connectionMutex`、`m_recyconnqueueMutex` 三把锁没有全序关系，不构成死锁预防协议，彼此之间不发生互斥。

**失效模式：**
- `send()` 在已关闭的 fd 上返回 `EBADF`
- 更危险：内核在 `send()` 使用旧 fd 值期间将其复用给新 accept 的连接，数据被发送到错误客户端

---

## 🟠 高 — 死代码 / 性能退化

### 3. `g_stopEvent` 从未被设为 1，关闭路径是不可达死代码

**文件:** `server/app/nginx.cxx:51` / `server/signal/ngx_signal.cxx` / `server/proc/ngx_process_cycle.cxx:163-165`
**类别:** 逻辑缺陷

`g_stopEvent` 全局变量：1 次写入（初始化为 0），5 次读取，0 次置 1。信号处理函数（`ngx_signal.cxx`）的 Worker 分支为空，不设置 `g_stopEvent`。

影响：
- `ServerSendQueueThread` — `while(g_stopEvent == 0)` 永远无法退出
- `ServerRecyConnectionThread` — 同上
- `ServerTimerQueueMonitorThread` — 同上
- Worker 主循环 `for(;;)`（`ngx_process_cycle.cxx:151`）永远无法走到第 163-165 行的清理代码：
  ```cpp
  g_threadpool.StopAll();
  g_socket.Shutdown_subproc();
  CMysqlConnPool::GetInstance()->Destroy();
  ```

**现实影响：** 进程被 SIGKILL 时操作系统回收资源，但优雅关闭完全不可能。且 `Destroy()` 中 `m_cond.notify_all()` 与部分归还的 `shared_ptr<MYSQL>` 存在竞争风险，如果未来实现了优雅关闭反而会暴露此问题。

---

### 4. O(n²) goto 重启循环，批量断开时性能退化严重

**文件:** `server/net/ngx_c_socket_conn.cxx:218-249` (lblRRTD), `:262-272` (lblRRTD2), `server/net/ngx_c_socket_time.cxx:113-125` (lblMTQM)
**类别:** 算法复杂度

在 `erase(pos)` 之后，代码用 `goto` 跳回 `begin()` 重新开始遍历整个容器。对于 N 个元素中 K 个被移除，迭代次数为 K×N - K(K-1)/2。当 K=N 时达到 ~N²/2。

**触发：** 批量断开（网络分区恢复、服务器关停、心跳超时大批踢人）。10000 个连接断开 = 约 5000 万次迭代。

**修复：** 使用 C++11 的 `pos = container.erase(pos)` 返回下一个有效迭代器，无需 goto，复杂度降为 O(N)。两处代码均应修改。

---

### 5. 每个业务数据包都同步阻塞调用 Redis EVAL

**文件:** `server/logic/ngx_c_slogic.cxx:220`
**类别:** 性能 — 热路径阻塞

`threadRecvProcFunc()` 是线程池的消息入口。每个通过 CRC 校验和序列号检查的有效消息，都**无条件**执行一次同步 `m_pRedis->eval<long long>(lua_script, ...)`——一次 Redis 网络往返（局域网 0.5-5ms）。

- 10K QPS → 每秒 10000 次 Redis 调用
- 120 个业务线程全部被串行化在 Redis I/O 上
- 吞吐量上限：120 × (1000/5) ≈ 24K QPS（Redis 延迟 5ms 时）
- 纯 CPU 处理能力可达 500K+ QPS，差距约 20 倍

L1 本地黑名单可减少已封禁 IP 的调用，但所有新 IP/低频 IP 的每个数据包都要穿透到 Redis。

**改进方向：**
- 添加本地滑动窗口计数器（`gettimeofday()` + 原子整数），仅在本地超阈值后才升级到 Redis
- 使用 `EVALSHA` + 预加载脚本减少每包传输的脚本字节数
- 周期性批量上报而非每包查询

---

## 🟡 中 — 性能 / 可维护性

### 6. `DeleteFromTimerQueue` 每次断开 O(n) 扫描整个定时器 multimap

**文件:** `server/net/ngx_c_socket_time.cxx:113-125`
**类别:** 数据结构选择

`m_timerQueuemap` 是一个 `std::multimap<time_t, LPSTRUC_MSG_HEADER>`。每次连接断开时，`DeleteFromTimerQueue` 从头扫描整个 map 寻找匹配的 `pConn` 指针。

- 50000 长连接 ⇒ 每次断开扫描 50000 次
- 1000 次同时断开 ⇒ 约 5000 万次迭代（结合 #4 的 O(n²) 行为）

**修复：** 添加 `std::unordered_map<lpngx_connection_t, multimap_iterator>` 反向索引，O(1) 查找 + O(log n) 删除。

---

### 7. `inRecyConnectQueue` 重复检测 O(n) 扫描整个回收列表

**文件:** `server/net/ngx_c_socket_conn.cxx:167-185`
**类别:** 不必要的线性扫描

每次 `zdClosesocketProc()` 将连接放入回收队列前，需要确认不在队列中。当前实现遍历整个 `m_recyconnectionList` 做指针比较。

- 200 disconnect/s × `m_RecyConnectionWaitTime=60s` = 约 12000 条目的回收列表
- 每次关闭 = 12000 次指针比较
- 扫描期间持有 `m_recyconnqueueMutex`，阻塞回收线程

**修复：** 在 `ngx_connection_s` 上添加 `bool m_inRecyQueue` 标志位，O(1) 判断。

---

### 8. MySQL 查询期间持有 `logicPorcMutex`，同一连接发生队头阻塞

**文件:** `server/logic/ngx_c_slogic.cxx:326,379,430,480`
**类别:** 锁粒度

`_HandleRegister`（第 326 行）和 `_HandleLogIn`（第 379 行）获取 `pConn->logicPorcMutex` 后执行 MySQL 查询（`RegisterUser` / `VerifyLogin`——可能耗时 100ms+）。在此期间同一连接的其他数据包（包括心跳 Ping）全部阻塞。

同时 `_HandlePing`（第 480 行）为仅设置 `lastPingTime = time(NULL)` 也获取了完整的互斥锁——在 x86-64 上这是一个原子 8 字节写入，用 `std::atomic<time_t>` 即可避免锁开销。

**触发：** 客户端在登录/注册后立即发 Ping。MySQL 延迟 200ms 期间 Ping 被阻塞，200ms 后释放，心跳未超时。但若网络和 DB 同时抖动，可能导致心跳超时误踢。

---

### 9. `m_total_connection_n` 只增不减，统计漂移

**文件:** `server/net/ngx_c_socket_conn.cxx:92,134` / `server/net/ngx_c_socket_conn.cxx:147-155`
**类别:** 计数器不一致

`m_total_connection_n` 在 `initconnection()`（第 92 行）和 `ngx_get_connection()`（第 134 行）中递增，但在 `ngx_free_connection()` 和 `clearconnection()` 中从不递减。一周运行后报告 "连接总数 1000 万"，而实际在池连接约 10000。

**现实影响：** `printTDInfo()` 打印误导性统计。连接限制守卫（`ngx_c_socket_accept.cxx:101`）使用 `m_connectionList.size()` 而非此计数器，因此不是安全漏洞——但运维和容量规划会基于错误数据做出决策。

---

### 10. Redis 连接参数和限流阈值全部硬编码在源码中

**文件:** `server/logic/ngx_c_slogic.cxx:138-143,211-231`
**类别:** 可部署性 / 架构

以下值硬编码，无法通过 `nginx.conf` 配置：

| 硬编码值 | 位置 | 当前值 |
|---------|------|--------|
| Redis 主机 | `CLogicSocket::Initialize():138` | `"127.0.0.1"` |
| Redis 端口 | `:139` | `6379` |
| Redis 连接池大小 | `:143` | `10` |
| 限流窗口 | `threadRecvProcFunc():213`（Lua 脚本中） | `60` 秒 |
| 限流阈值 | `:227` | `20` 次请求 |
| 黑名单封禁时长 | `:231` | `60` 秒 |

`nginx.conf` 的 `[Cache]` 部分**仅**控制 TTL（`UserInfoCacheTtlSec`、`NullUserCacheTtlSec`）。修改 Redis 地址或调整限流策略需要改源码 + 重新编译。

另外，`EVAL` 的第 3 个参数 `{"60"}` 传入 Lua 的 `ARGV` 数组，但 Lua 脚本本身并没有读取 `ARGV[1]`（过期时间 `60` 写在脚本代码里而非从参数获取），对维护者形成误导。

---

## 汇总

| 严重程度 | 数量 | 涉及 |
|---------|------|------|
| 🔴 严重 | 2 | Redis fork 共享 fd、send/close fd 竞争 |
| 🟠 高 | 3 | g_stopEvent 死代码、O(n²) goto、每包阻塞 Redis |
| 🟡 中 | 5 | O(n) 扫描 ×2、锁粒度、计数器漂移、硬编码配置 |

**最紧急修复：Issue #1（Redis fork 共享 fd）**——当前配置 `WorkerProcesses=4` 即可触发，Redis 缓存和限流全部静默失效。
