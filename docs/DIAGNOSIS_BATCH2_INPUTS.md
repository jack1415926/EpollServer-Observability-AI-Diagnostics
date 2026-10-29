# 诊断第二批：知识分层与最小运行证据

历史批次及当前更正（2026-10-30）：本页T4～T6/T8初版已交付；六卡片现为显式catalog模式，默认使用分层current语料；T7已有CPU窗口利用率。见[main整合](MAIN_MERGE_2026-10-29.md)及[统一状态](CURRENT_STATUS.md)，下文23片段等数字只代表当批。

2026-10-25。按用户要求先将技术解释错误、过度表达未知、输出契约失败等登记为[优化待办](DIAGNOSIS_OPTIMIZATION_BACKLOG.md)，本批实施T4～T8的第一个有界增量。第一批案例、原始分数和内部复核不改写，不根据保留集分数选择检索参数或系统提示词；已经审阅过的案例不能再称为完全未见，后续泛化验证仍需新保留案例。

## 知识来源和版本

`diagnosis/knowledge/catalog.json`显式列出6份知识卡片，当前共23片段：12个当前实现契约、7个技术原理、4个历史案例片段。片段少不表示覆盖充分；目标是先形成可审查的主知识库，不以文件数量验收。

| 类型 | 内容 | 证据性质 |
|---|---|---|
| current_contract | 运行采集、事件、实验契约 | 当前实现规则，不是本次健康证明 |
| technical_reference | Linux进程/epoll、MySQL锁等待、Redis延迟 | 一手资料的简短释义，保留URL、核对日期与适用范围；不是当前实例观测 |
| verified_incident | 日志格式、快照身份误报、测试资源生命周期 | 基于fbebd4f的确认过的历史修复和测试；不自动吸收模型报告 |

来源已核对：Linux [proc_pid_stat(5)](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html)、[proc_pid_task(5)](https://man7.org/linux/man-pages/man5/proc_pid_task.5.html)、[epoll(7)](https://man7.org/linux/man-pages/man7/epoll.7.html)、[Linux v6.17的proc实现](https://raw.githubusercontent.com/torvalds/linux/v6.17/fs/proc/array.c)、[MySQL 8.4 data_lock_waits](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-data-lock-waits-table.html)、[Redis延迟监控](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/latency-monitor/)。没有整页抓取入库，没有修改MySQL/Redis配置。实际/proc验证环境为Linux 7.0.0-38-generic，旧版本源码只用于语义核对，不假称与本机源码逐字相同。

每条卡片记录kind、authority、revision、applies_to、sources、catalog本身和本地支持代码/测试文件的SHA-256。检索结果分别标记source_changed、supporting_source_changed、catalog_changed，避免说明没变而实现或分类策略已变时悄悄继续当作当前契约。外部网页没有声称存在已保存的完整页面快照或动态版本核验；checked_on仅代表本轮阅读日期。

默认建库选择current显式目录。README、阶段进度、规划性质记录和上游旧笔记保留在legacy入口，不混进主库。没有删除原文；历史案例本身仍可检索，但明确标成历史。

```bash
# 当前卡片独立建库，不覆盖旧基线
.venv/bin/python -m diagnosis.retrieval --corpus current \
  --output artifacts/diagnosis-batch2/index-current.npz
# 如需旧资料，明确生成另一个索引
.venv/bin/python -m diagnosis.retrieval --corpus legacy \
  --output artifacts/diagnosis-batch2/index-legacy.npz
```

服务由操作者固定选择索引，用户/模型不能指定路径：

```bash
export EPOLL_DIAG_INDEX_PATH="$PWD/artifacts/diagnosis-batch2/index-current.npz"
.venv/bin/python -m diagnosis.service
```

原始291片段索引另存`artifacts/diagnosis-evaluation/baseline-v1/index-frozen.npz`，本轮23片段索引与原索引分开。增量构建按模型与正文复用向量，最后更新来源链接时只重新嵌入1片段、复用22片段。没有因片段更少就宣称效果更好，尚未取得同一独立金标下的新旧质量对照。

## 独立于业务快照的进程资源

C++读取器复用现有/proc身份读取和最终复核，在每个Worker行增加process_resources；不修改TCP收发热路径、业务快照发布器或故障策略。

| 字段 | 含义和限制 |
|---|---|
| identity_verified / reason / status | PID、启动刻度、父进程已核验，资源可完整/部分/不可用；与业务metrics有效性分开 |
| leader_state | 主线程的/proc状态，如T停止、S可中断睡眠；不代表每个线程的忙闲 |
| cpu_user_ticks / cpu_system_ticks | 累计CPU时间，包含进程线程组口径，不含已回收子进程时间；不是利用率 |
| clock_ticks_per_second | 系统tick频率，便于换算累计时间；频率不可用时保留null |
| rss_bytes / rss_is_approximate | 按系统页大小换算的近似RSS；不是PSS或精确内存 |
| threads | 线程数量，不是正在处理请求的线程数 |
| observed_at_boottime_ms | 本轮资源观测时间；不改写旧业务快照时间，不声称跨字段事务采样 |

CPU用户态/内核态、线程数或RSS字段缺失/非法/溢出时对应值为null，不冒充0。身份变化、进程退出等核验失败不接受旧生命周期资源。过期业务样本仍保持stale和缺失指标；如果同一进程身份仍有效，可以同时返回新鲜的独立进程观测。新进程字段不作为新增异常规则，也不把T直接升级为Worker退出。

unsupported_metrics中的cpu_memory调整为cpu_utilization；现在有近似RSS和累计CPU时间，仍无区间CPU利用率、业务耗时、累计错误或数据库内部指标。历史阶段说明保留当时口径。Worker页新增进程状态、RSS和累计CPU秒数，并明确其边界；过期缓存仍按原方式失效。

## 实验工具和测试输入

新增第八种只读工具get_experiment：仅接受UUID形式的experiment_id，读取已有鉴权GET详情接口，返回原条件和资源摘要并标为historical_experiment_detail。没有额外SQL写操作、任意文件/URL或新数据库权限。

完整阶段5验收改为显式传入`--reports`，至少两份既有报告；默认不再绑定某台机器的artifacts目录。目录缺失在启动数据库/TCP前拒绝。来源记录绝对路径，允许工作区外已有报告；不改写其内容或称为新测量。内部Python子服务沿用启动脚本的sys.executable，不要求特定位置有另一份虚拟环境。

```bash
.venv/bin/python testscript/test_stage5_live.py \
  --reports /path/to/existing-experiments
```

本轮只验证新详情适配器的UUID/边界、既有HTTP接口兼容及脚本启动前校验，没有因路径变化重跑数据库或性能负载。完整MySQL持久化联调可按上述命令独立重跑。

## 验证与资源状态

| 范围 | 实际结果 |
|---|---|
| 构建 | CLI与HTTP Make目标C++17、-Wall/-Wextra/-Werror通过；JS语法和diff检查通过 |
| 读取器 | 19项通过，包括3项资源字段边界、真实子进程SIGSTOP/SIGCONT和已有读取器/事件组合场景 |
| 诊断 | 16项既有回归通过 |
| 第二批与评测框架 | 5项新知识/工具/脚本测试＋10项第一批评测完整性测试通过 |
| HTTP、事件、导入与生命周期 | 24项通过 |
| 测试进程组退出与失败保留 | 5项通过 |
| 远端知识链路 | 23片段真实Embedding构建、远端检索/重排、一次捕获观测诊断报告通过；不是诊断效果评测 |
| 浏览器 | Worker资源列显示正确，断开清空，无脚本错误，无令牌持久化；桌面axe零违规/待检查；390px无整体溢出、表格可键盘横向滚动 |

共79个不同回归用例，分组运行，不称为一次79项合并执行。窄屏axe零自动违规，资源列被裁切造成5个对比度待检查节点；已横向滚动并结合相同桌面样式与截图核对，不宣称全站WCAG认证。

最终最小联调入口：

```bash
.venv/bin/python -m unittest testscript.test_diagnosis_batch2 -v
PYTHONPATH=testscript python3 -m unittest test_metrics_collector -v
# 真实/proc和Drogon，受控业务快照，无TCP/MySQL负载
.venv/bin/python -B testscript/probe_diagnosis_batch2.py
# 可选付费模型；--hold要求交互终端，回车清理
.venv/bin/python -B testscript/probe_diagnosis_batch2.py --online --hold
```

最终证据在`artifacts/diagnosis-batch2/evidence-btixrlne/`：真实睡眠替身进程暂停后，受控业务样本过期而进程观测为T，继续后恢复fresh；使用实际四接口响应捕获回放（监控未配置数据库时事件/实验返回503），模型能说明观测到T、累计CPU不等于当前利用率，并保留未证明的因果。不是实际TCP业务故障或根因准确率证据。

网页截图和初次探针在`artifacts/diagnosis-batch2/evidence-udip5h7n/`。初次探针的events/experiments为空回放值，已在产物注明，不当成数据库验证；最终脚本改为捕获实际接口。两次探针均用私有进程组，确认退出后保留证据；浏览器、Drogon、睡眠替身和发布线程均已退出。没有删除未知临时目录或操作原MySQL/Redis/TCP数据。

## 后续范围

T4～T6与T8完成初版；T7完成本批选择的进程状态/RSS/CPU累计观测，区间CPU、业务耗时、DB等待和日志时间窗/实例归属仍需后续小批。O1～O8不自动关闭；SYSTEM诊断规则未改写，检索参数未调优，保留集未重跑或调参。正式部署、正式监控开销与原负载性能评估未执行。本批与第一批评测改动一并纳入本次授权提交，范围见DECISIONS。
