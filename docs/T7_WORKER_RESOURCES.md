# T7第一批：Worker CPU与RSS

范围：独立C++监控进程读取Linux `/proc/PID/stat`，沿用Master/Worker身份核验。TCP业务及文件快照格式不变；不增加线程级分析、请求耗时、资源告警或自动修复。此批只完成T7的CPU/RSS子项。

## 数据契约

`workers[].resources`独立于`workers[].metrics`业务快照。字段如下：

| 字段 | 含义 |
|---|---|
| `source` | `linux_proc_stat` |
| `status` / `reason` | 资源可用性available/partial/unavailable及原因，与业务快照fresh/stale分别判断 |
| `cpu_percent` | 相邻资源采样窗口的进程CPU平均使用率，100%表示一个逻辑CPU，可超过100%；不是整机CPU占比 |
| `cpu_percent_basis` | `one_logical_cpu_100_percent` |
| `rss_bytes` | stat第24字段RSS页数乘系统页大小，近似常驻内存字节，不是精确独占内存或泄漏证明 |
| `interval_ms` | CPU差值窗口长度，首次或不能计算时null |
| `sampled_at_unix_ms` / `sampled_at_boottime_ms` | 资源样本的墙钟/同启动时钟时间，与业务快照时间分别记录 |

CPU采用stat第14/15字段utime+stime之差，除以`sysconf(_SC_CLK_TCK)`和BOOTTIME窗口秒数，再乘100。不计子进程cutime/cstime，不重复计guest_time，不按整机核数归一化。HTTP沿用单后台线程约500ms采样；GET只读缓存，不自行采样或重置基线。

基线限定监控采样器、proc来源、boot ID、Master PID/start_ticks及Worker PID/start_ticks。Worker移出名单、资源读取失败、身份不符或实例无效时清除相关基线。CPU计数回退则当前CPU为null，重新建立基线，不报告负值。首次CPU为null、reason=cpu_warming_up，RSS可单独可用；短命CLI只采一次，因此CPU正常也是null。不能用null冒充0。

进程资源读取在采样前后核验Worker身份，并受整次名单复核和HTTP缓存失效约束。`status=stale`只描述业务快照；暂停进程的业务快照停止更新，CPU/RSS仍可从内核取得。RSS不要求业务快照fresh。资源读取失败不改变原三类事件规则或业务指标汇总语义；本批不提供CPU/RSS跨Worker总量。

当前可观测Worker进程CPU和近似RSS；线程状态、热点函数、请求耗时、累计业务/错误计数及MySQL/Redis内部指标仍未支持。高CPU、低CPU或RSS增长只缩小排查范围，不能确认CPU热点、锁等待、DB等待或内存泄漏；也不能证明TCP业务健康。

字段核对来源为本机Linux man-pages的`proc_pid_stat(5)`与实际/proc最小探针；手册明确stat RSS值不精确。本批选低开销近似值，不读取smaps做高成本精确统计。

## 运行与验收

构建沿用`make -C monitor`及`make -C monitor http`；已有监控启动方式不变。新字段随`/api/v1/workers`及原诊断`get_worker_metrics`工具返回。页面显示CPU百分比、RSS MiB及资源采样状态/时间，首次CPU显示「—」。资源问题按需带入本页契约，不增加工具权限。

复测入口：

```bash
PYTHONPATH=testscript python3 -m unittest test_metrics_collector test_event_rules test_http_monitor -v
node testscript/test_diagnosis_render.js
PYTHONPATH=testscript EPOLL_T7_EVIDENCE=artifacts/t7-new python3 -m unittest \
  test_http_monitor.HttpMonitorTests.test_worker_resources_cpu_load_pause_resume_and_exit -v
# 现有业务库可连接；使用隔离Ping配置，本批不依赖Redis服务
PYTHONPATH=testscript python3 testscript/test_http_monitor_live.py --resources --output artifacts/t7-tcp-new
```

诊断服务相关异步测试使用允许本地唤醒的执行环境和有界监督。真实联调启动本轮私有进程组，确认整组退出后才删除含凭据的临时配置；原服务不按名称批量终止。

## 本批验证记录（2026-10-28）

忽略目录`artifacts/t7-resources/`保存原始HTTP样本、代码/二进制哈希、截图与测试日志。它们是本批功能证据，不是正式性能对照。

- 读取器18项测试通过，包含首次CPU、差值公式/多核口径不截断、RSS页换算、独立于stale业务快照、计数回退、资源字段异常、实际身份不符、进程消失、名单移除与基线重建，以及原读取器/事件规则组合回归。
- 真实HTTP与受控进程：分配内存并执行CPU负载，CPU可观测；SIGSTOP后CPU为0、RSS仍可读，而业务快照stale且业务指标null；SIGCONT后CPU恢复；退出后CPU/RSS为null。原始样本为`controlled-worker.json`。高于100%的短窗口读数受时钟滴答粒度影响，不据此声称该单线程进程长期占用多个核心。
- 真实TCP Worker：复用现有二进制及本机业务库，用私有2 Worker×4线程、独立端口和目录完成Ping连接数0→1→0、资源初始读取、暂停/恢复、HTTP失效不影响TCP、Worker意外退出导致整体退出且HTTP仍存活。只在临时配置关闭Redis限流，不读写原Redis数据，不做注册/业务数据库写入。证据在`tcp-live/`；测试配置在本轮TCP组退出确认后删除。
- 页面：桌面和390px窄屏展示CPU/RSS与采样状态，页面宽390px、文档宽390px；表格在316px容器内横向滚动至696px内容。首轮发现滚动区缺少键盘焦点，修复后浏览器复测与axe检查为0违规、1项短文本对比度待人工核对，不宣称自动审计覆盖一切。截图`desktop.png`、`mobile.png`和`mobile-resources.png`；旧失败审计保留为`a11y-initial.json`。
- 生产`Tools.get_worker_metrics`实际读取隔离HTTP，CPU/RSS原值定位检查与资源契约读取通过，证据`tool-evidence.json`。新增diagnose组合回归验证资源问题按观测回答、不进入Agent循环；未进行付费模型评测或宣称新的诊断准确率。

浏览器前两次手工停留超过自动清理期限，出现连接拒绝；不计页面通过。后改为一个有界脚本管理夹具、浏览器和清理，完成上述验收，没有为UI重跑TCP或压测。

## 未覆盖范围

未做正式监控开销对照、大规模压测、数据库持久化/SQL重启、长期资源趋势或内存泄漏评估，也未部署或推送。RSS是近似值；CPU/RSS有效不代表业务健康。T7线程/请求/DB证据子项仍在待办，不将首批资源观测写成整个T7完成。T11/T12残余语义待办保留。
