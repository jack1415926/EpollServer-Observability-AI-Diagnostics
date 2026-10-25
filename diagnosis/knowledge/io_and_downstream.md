# I/O与下游等待：需要什么证据

## epoll就绪与业务完成

来源：Linux man-pages epoll(7)，2026-10-25核对。
https://man7.org/linux/man-pages/man7/epoll.7.html

epoll报告文件描述符I/O就绪，不报告业务已完成。ET使用非阻塞I/O并处理至EAGAIN的语义，与应用接收队列积压原因不同；看到队列条数不能直接证明epoll有问题。

## MySQL锁等待与客户端连接池

来源：MySQL 8.4 Reference Manual，data_lock_waits，2026-10-25核对。
https://dev.mysql.com/doc/refman/8.4/en/performance-schema-data-lock-waits-table.html

data_lock_waits关联被阻塞的数据锁请求与阻塞者，可解释对应时段的锁依赖；它不是本项目客户端连接池的借用队列。连接池耗尽需要池容量、借用和等待等证据，不能仅由TCP连接数或MySQL锁表猜定。本批没有执行数据库查询或启用新采集。

## Redis延迟与限流触发

来源：Redis Open Source Latency Monitoring，2026-10-25核对在线文档，功能自2.8.13引入。
https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/latency-monitor/

延迟监控按事件记录超过阈值的慢路径，可辅助检查命令或持久化等开销。延迟记录不等于应用Lua脚本拒绝计数；缓存击穿还需命中/未命中、过期及回源证据。本批不修改Redis配置；当前监控未接入这些指标，文档只说明后续验证方向。
