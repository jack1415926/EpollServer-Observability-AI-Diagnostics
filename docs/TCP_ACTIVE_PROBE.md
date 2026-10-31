# 主动TCP Ping探测

2026-10-31。作为T7运行证据的新增交付，独立Drogon服务通过现有TCP Ping协议验证本机服务响应，将结果接入HTTP、总览页面和诊断工具。

## 使用

```bash
make -C monitor http
export EPOLL_MONITOR_TCP_PORT=18080  # 被监控TCP实例的端口
export EPOLL_MONITOR_TCP_INTERVAL_MS=5000
export EPOLL_MONITOR_TCP_TIMEOUT_MS=1000
# 沿用已有访问令牌和明确的Master名单
monitor/build/epoll-monitor /absolute/path/to/master-manifest.json 8080
```

设置`EPOLL_MONITOR_TCP_PORT`后启用；不设置时为`not_probed/disabled`。目标固定为127.0.0.1，端口范围1～65535。间隔默认5000ms、允许100～60000ms；总超时默认1000ms、允许10～5000ms且不超过间隔。配置非法时启动返回64。

独立后台线程核验Master的boot ID、PID和启动刻度，再将`/proc/PID/fd`中的socket inode与`/proc/PID/net/tcp`监听记录匹配，确认该端口由当前Master持有。发送8字节Ping，完整接收并核对包长、命令字和CRC；连接、发送及接收共用一次超时。响应后再次核对进程身份及监听归属。

每轮结束后等待配置间隔，HTTP只读缓存。停止服务时唤醒等待中的线程，正在进行的网络请求按总超时退出。探测连接属于普通TCP连接，可能短暂计入在线连接数。

## HTTP和页面

health、overview、workers和metrics均提供`tcp_health`及`tcp_probe`。health的`monitor`表示HTTP监控存活；`collection_status`及响应`validity/data_valid`继续表示基础指标采集状态。

| 字段 | 含义 |
|---|---|
| tcp_health / tcp_probe.status | reachable、unreachable、unknown或not_probed |
| tcp_probe.reason | 本次成功或失败原因 |
| rtt_ms | 成功Ping的连接及协议往返耗时；失败或过期时为null |
| checked_at_unix_ms / age_ms | 本次核验完成时间及缓存年龄 |
| host / port | 固定本机地址及配置端口 |
| interval_ms / timeout_ms / max_age_ms | 间隔、单次总超时和结果有效期 |
| instance | 被核验Master的boot ID、PID和启动刻度 |

| 结果 | 状态和原因 |
|---|---|
| 协议响应正确 | reachable / ping_ok |
| 已核验实例没有目标监听 | unreachable / not_listening |
| 连接、协议或网络失败 | unreachable / connect_failed、timeout、protocol_error、connection_closed或socket_error |
| 端口属于其他实例 | unknown / target_mismatch |
| 实例身份不可用或变化 | unknown / identity_unavailable、identity_changed或target_unavailable |
| 缓存过期 | unknown / stale；清空成功耗时 |
| 未配置端口 | not_probed / disabled |

缓存有效期为`2×interval_ms+timeout_ms`，按BOOTTIME计算。基础采集切换或失去Master身份时立即停止展示旧实例成功结果。页面分别显示监控可达、TCP响应、往返耗时、最近探测及原因，断开时清空结果。

Ping验证一次本机协议往返；逐Worker响应和MySQL/Redis业务操作使用各自证据。首批通过总览展示探测结果，事件规则沿用原三类。

## 诊断接入

`get_server_health`沿用原接口，返回含探测状态的health证据。路由说明已加入TCP Ping响应、耗时和失败原因；固定能力契约与运行观测知识同步更新。离线检索索引按现有流程更新后加载。

## 验证入口

```bash
PYTHONPATH=testscript .venv/bin/python -m unittest \
  test_tcp_probe test_tcp_probe_http test_http_monitor -v
node testscript/test_diagnosis_render.js
# 私有TCP/Drogon实例，Ping配置关闭Redis限流，沿用现有业务MySQL连接
python3 testscript/test_http_monitor_live.py --resources --tcp-probe \
  --output /absolute/path/to/new-evidence-directory
```

探针与HTTP合并回归24项通过：真实分片响应、协议字段、超时/关闭、身份/监听归属、缓存过期及实例隔离、故障恢复、HTTP缓存读取、生产诊断工具读取和原HTTP行为。诊断路由、语料、知识目录和证据契约47项回归通过，含Ping事实生成、报告目标实例绑定和旧提示清除；最后单独复核了真实HTTP到诊断目标身份的映射。JS语法及页面渲染回归通过。

真实TCP完成正常Ping、全部Worker暂停后超时、恢复后成功、单Worker快照过期恢复、错误名单、HTTP退出后TCP仍响应，以及Worker崩溃后原整体退出行为；TCP退出后health保持200、monitor=ok、tcp_health=unknown。

浏览器使用独立agent-browser会话验证桌面、390px窄屏及断开清空；页面宽度和文档宽度均为390px，axe-core 4.12.1扫描零违规/待检查。截图及原始JSON保存在`artifacts/tcp-probe-live-20261031/`，脱敏数字、来源哈希和状态流转见[验收摘要](../evaluation/tcp_probe_20261031/acceptance.json)。

本批TCP/Master/Worker、HTTP和浏览器已退出，TCP/HTTP端口释放，临时TCP配置删除。原MySQL保持PID2305/start_ticks1041、3306可达，本机nginx.conf哈希未变。此轮复用原业务MySQL连接执行Ping测试，未创建监控数据库。

本批属于功能验收；此前监控开销数字仍对应其记录的二进制和负载方法。
