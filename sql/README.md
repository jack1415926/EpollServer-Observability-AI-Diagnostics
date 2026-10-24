# MySQL 持久化联调说明

## 1. 安装依赖（Ubuntu / Debian VM）

```bash
sudo apt update
sudo apt install -y mysql-server libmysqlclient-dev
sudo systemctl start mysql
```

## 2. 建库建表与业务账号

```bash
cd ~/Learn_EpollServer-main/sql
sudo mysql -u root -p < init_users.sql
```

默认创建：

- 库：`epoll_db`
- 用户：`epoll_user` / `epoll_pass`（与 [`server/nginx.conf`](../server/nginx.conf) `[Mysql]` 段一致）

## 3. 配置 nginx.conf

确认 `[Mysql]` 段与上面账号一致，例如：

```ini
[Mysql]
MysqlHost = 127.0.0.1
MysqlPort = 3306
MysqlUser = epoll_user
MysqlPassword = epoll_pass
MysqlDatabase = epoll_db
MysqlPoolSize = 16
```

## 4. 编译与启动

```bash
cd ~/Learn_EpollServer-main/server
make clean && make
./nginx
```

日志中应出现：`CMysqlConnPool::Init() 成功`（每个 Worker 一条）。

**注意**：连接池在 **Worker fork 之后** 初始化，不要在 `main()` 里 Init。

## 5. 验证数据

注册/登录成功后：

```bash
mysql -u epoll_user -pepoll_pass -e "SELECT id,username,created_at FROM epoll_db.users;"
```

## 6. 回包错误码（`iType` / `iResult`）

| 值 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | 用户名重复（注册） |
| 2 | SQL/业务失败 |
| 3 | 连接池获取超时 |

## 7. 连接数估算

`WorkerProcesses × MysqlPoolSize` = 总 MySQL 连接上限（例如 4×16=64），请保证 `max_connections` 足够。

## 8. GetUserInfo 与 Cache-Aside

命令码 `_CMD_GET_USER_INFO`（`msgCode = 7`）。

| 包体 | 说明 |
|------|------|
| 请求 | `int64_t userId`（网络大端） |
| 响应 | `iResult`(4) + `userId`(8) + `username[56]` |

**`iResult`（GetUserInfo）**

| 值 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | 用户不存在（含空值缓存命中） |
| 2 | 参数非法 |
| 3 | Redis/MySQL 不可用或连接池超时 |

**Redis 键**（与 L2 限流 `RateLimit:{ip}` 隔离）：

- 正常：`user:info:{userId}` → `USER_V1:1001:alice`，末尾用户名原样保存（包括引号、冒号等），TTL 默认 3600s（`[Cache] UserInfoCacheTtlSec`）。旧 JSON 视为未命中，回源后刷新，无需批量删除缓存。
- 穿透保护：值 `NULL_USER`，TTL 默认 60s（`NullUserCacheTtlSec`）

SQL 错误返回后端故障，不写 `NULL_USER`；只有成功执行查询且确实无记录才缓存空值。MySQL 池失效槽位在后续借用时按需补建；排队等待最多 5 秒，新建连接另受 MySQL 连接超时限制。停机须在业务线程结束后执行 `Destroy()`，该函数等待已借出及在途连接完成，随后才关闭 MySQL 库。

```bash
# 在仓库根目录执行；先确保 users 表有数据
python3 testscript/test_register_login.py
python3 testscript/test_get_user_info.py --exist-id 1 --missing-id 99999

redis-cli GET user:info:1
redis-cli TTL user:info:99999
```

## 9. 面试要点

- `std::shared_ptr<MYSQL>` + 自定义 Deleter：作用域结束 **归还队列**，不 `mysql_close`
- `std::unique_lock` + `condition_variable::wait_for`：空闲连接等待与超时
- 预处理语句 `MYSQL_STMT`：防 SQL 注入
- fork 后 per-worker 独立连接池
- Cache-Aside：先 `GET user:info:{id}`，未命中查库后 `SET`；无用户写 `NULL_USER` 短 TTL 防穿透

## 10. 独立监控实验表（阶段 3）

`sql/monitor.sql` 由管理员应用到独立监控数据库，创建 `monitor_experiment`，不修改上述业务表。HTTP 监控账号仅需该表的 SELECT、INSERT，不需要 CREATE、UPDATE 或 DELETE。监控服务通过 `EPOLL_MONITOR_DB_*` 环境变量连接，不读取 TCP 业务凭据；未配置数据库仍可使用实时监控。

初始化、启动、实验导入和查询方法见 [阶段 3 Web 与性能实验](../docs/STAGE_3_WEB_EXPERIMENTS.md)。
