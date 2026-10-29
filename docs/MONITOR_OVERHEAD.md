# 正式监控开销对照

2026-10-30。用户授权按同口径完成开销评估，并在正式测量前确认本批目标：完整监控相对关闭组的QPS中位数下降不超过5%、抽样P95中位数上升不超过10%，请求零失败。目标用于判断这一批观测，不保证生产或其他负载下的开销。

## 对照条件

| 条件 | Worker快照 | Drogon、CPU/RSS和事件 | HTTP读取 |
|---|---|---|---|
| off | 关闭 | 不启动 | 无 |
| snapshot | 开启，约1秒一次 | 不启动 | 无 |
| full | 开启，约1秒一次 | 约500ms采集，默认事件窗口/阈值，私有MySQL持久化 | 每2秒读取overview、workers、events各一次 |

full对应正常实时监控及事件页轮询。AI模型请求、诊断任务、实验导入/比较、持续事件写入和故障注入不作为负载；已有真实报告仅在首个full轮正式负载前做一次写入/读回预检。该范围不能称为所有产品操作同时运行的最坏开销。

同一TCP二进制及本机配置派生的临时配置，4 Worker × 120业务线程、每Worker MySQL池16、连接上限10000。仅改变快照开关及是否启动监控/轮询；保留本机数据库凭据，不输出配置正文。Make DEBUG=true，沿用现有Debug构建。

- TCP及Drogon固定CPU 0–3，客户端固定CPU 4–11；轮询线程也固定CPU 4–11。监控与TCP共享四核，不额外分配监控专用核。
- 同机loopback、源地址127.0.3.1，6客户端进程、总并发200，每连接10000次Ping，每正式轮200万次。
- strict / into / kernel-timeout、超时5秒、每100次请求抽样延迟。P95中位数是三轮各自抽样P95的中位数，不是合并分位数。
- 各轮单独启动TCP；每轮预热10万次，正式结束确认退出及端口释放后等待1秒。
- 顺序为off / snapshot / full / full / off / snapshot / snapshot / full / off，每条件三轮，各条件所处轮次平均值均为5。
- 独立Redis绑定原代码固定的127.0.0.1:6379，启动前必须确认空闲；无持久化，不加载原数据。Lua开启、60秒窗口、阈值1亿。独立监控MySQL与Redis在三种条件期间均保持运行，继承执行器CPU亲和性；业务MySQL仅供原Worker初始化连接，Ping不执行注册或业务SQL写入。
- 进程资源每500ms采样；full另按2秒轮询周期记录监控和私有监控MySQL资源，以及真实HTTP结果。后者区分预热与正式阶段，首次CPU为null。

历史[阶段0结果](BASELINE_RESULTS_2026-10-23.md)仅用于核对负载口径。本批以新off组作为分母，不用旧QPS计算开销。当前内核/环境、二进制、脚本与输入哈希见本批manifest；不把旧结果改写为当前代码的新测量。

## 执行与证据

先构建，再验证已有生命周期及契约回归：

```bash
make -C server
make -C monitor http
PYTHONPATH=testscript python3 -m unittest test_monitor_overhead test_process_group \
  test_stage3_lifecycle test_tcp_stress test_performance_sampler -v
python3 testscript/test_stage3_live.py --database-only
```

运行真实负载须使用允许本机网络和真实进程身份核验的环境。显式提供已有真实实验报告，字段错误或路径缺失必须在启动实例前拒绝：

```bash
# 短负载仅验证链路，每条件正式2万次、预热1万次，不计算正式达标结果
python3 testscript/benchmark_monitor_overhead.py --smoke \
  --report /absolute/path/to/round-1-experiment.json
# 正式九轮：1800万次，另预热90万次，原始产物保存到唯一的新目录
python3 testscript/benchmark_monitor_overhead.py \
  --report /absolute/path/to/round-1-experiment.json
```

原始产物保存在忽略的`artifacts/monitor-overhead-{smoke,formal}-*/`，不提交原始日志、资源数组、凭据或临时配置：

| 文件 | 内容 |
|---|---|
| manifest.json | 执行起始提交、系统/Python、二进制与脚本/报告哈希、顺序和预先确认的目标 |
| owned-services.json | 本批Redis/MySQL的PID、启动刻度、端口及数据目录 |
| 每轮context.json | 本批Master/Worker身份、端口、绑核及精确命令 |
| 每轮warmup/measured.json | 原始客户端结果，包含错误、抽样方式和数量 |
| 每轮warmup/measured-resources.json | 资源采样及系统摘要 |
| full轮http-samples.json | 分阶段HTTP返回、监控/监控库资源、轮询失败记录 |
| 每轮cleanup.json | TCP/Worker退出、端口释放和临时配置删除确认 |
| runs.json / summary.json | 已完成轮次、每条件中位数和范围、阈值判断及最终清理状态 |

汇总可独立离线重算，不启动服务或产生新负载：

```bash
PYTHONPATH=testscript python3 - /absolute/path/to/formal/runs.json <<'PY'
import json, sys
from pathlib import Path
from benchmark_monitor_overhead import summarize
print(json.dumps(summarize(json.loads(Path(sys.argv[1]).read_text())), indent=2))
PY
```

每轮结果即时落盘。任何请求失败、采集不完整、检测未运行、资源退出未确认均使脚本失败，现场保留，不通过重复抽样挑选成绩。不得在仍有本轮进程运行时重新启动同一测试。

退出码0表示测量及清理完成，不表示性能目标通过；验收必须读取summary.json中full.within_target（位于groups内）。本批该值为false。

私有MySQL沿用`TestDatabase`，管理员连接SHUTDOWN并wait后删除数据目录；Redis、TCP、HTTP只停止本轮创建且经身份核验的进程组。TCP组及端口退出确认前不删除其配置。退出失败保留目录和诊断信息，独立资源由ExitStack分别尝试清理。

## 本批结果

构建通过；26项针对性回归、私有MySQL真实生命周期预检通过。短负载三条件共9万次Ping零失败，完整监控检测/存储为running/ok，已有真实实验写入/读回通过；所有本批资源退出，本机配置哈希未变。

正式九轮已完成，1800万次请求及90万次预热均零失败；每正式轮取得2万条延迟样本，共18万条。原始目录为`artifacts/monitor-overhead-formal-3cqzcz2j/`，短负载目录为`artifacts/monitor-overhead-smoke-3w184e8e/`。可提交的数字、来源哈希及核验范围见[验收摘要](../evaluation/monitor_overhead_20261030/acceptance.json)。代码基点为`e716e25`加本批未提交脚本，Linux 7.0.0-38-generic、Python 3.14.4；不宣称工作树干净。

| 轮次 | 条件 | QPS | 抽样P95 ms |
|---|---|---:|---:|
| 1 | off | 35632.517 | 9.152 |
| 2 | snapshot | 36523.936 | 9.352 |
| 3 | full | 27557.525 | 10.761 |
| 4 | full | 27464.863 | 10.563 |
| 5 | off | 31776.939 | 9.829 |
| 6 | snapshot | 31835.731 | 10.018 |
| 7 | snapshot | 33493.499 | 9.421 |
| 8 | full | 31435.422 | 10.367 |
| 9 | off | 31933.946 | 10.322 |

| 条件 | QPS中位数 | 抽样P95中位数 ms | QPS相对off变化 | P95相对off变化 | 本批目标 |
|---|---:|---:|---:|---:|---|
| off | 31933.946 | 9.829 | — | — | 对照 |
| snapshot | 33493.499 | 9.421 | +4.88% | -4.15% | 数值落在目标内 |
| full | 27557.525 | 10.563 | -13.70% | +7.47% | 吞吐未达标，延迟达标，整体未达标 |

这里的snapshot更快不证明快照带来提速。off的QPS范围为31776.939～35632.517，snapshot为31835.731～36523.936，full为27464.863～31435.422；存在明显轮间波动，三轮不能提供稳定因果归因或生产开销上界。本批不丢弃较慢轮次，不追加采样挑选通过成绩。

完整监控三轮分别有36、36、31个正式阶段轮询周期，均完整采集、事件检测running、存储ok，无轮询错误。按2秒周期采样得到的监控进程平均CPU分别为0.47%、0.49%、0.47%（100%为一逻辑CPU），最大RSS约16.95～17.53MiB。私有监控MySQL同口径平均CPU为0.48%～0.54%。低平均CPU不能解释或否定吞吐差异；未采集调度延迟、逐线程热点、虚拟机宿主负载或业务等待证据，性能原因未确认。现有采样的steal平均值均为0也不能证明宿主完全没有干扰。

下一步定位若继续，先将full中的后台采集、数据库/事件与HTTP轮询做受控分项，保持TCP负载和CPU配额一致，验证具体机制后再决定优化。CPU分配改变属于另一个实验，不能与本批数字混算；已有测量保留，不将一次短探针冒充新的正式九轮验收。

## 清理与交付边界

九轮Master/Worker/Drogon的原启动身份均已独立核对为退出，全部测试TCP/HTTP端口释放、临时配置删除。独立Redis已退出且6379释放；私有MySQL经管理员SHUTDOWN并wait退出，数据目录删除、端口释放。原MySQL PID2398仍运行且3306可达，本机nginx.conf哈希未变；原业务数据未执行写入。原始性能证据保留，不删除旧artifacts。

本批交付可重跑脚本、匹配负载的测量及未达标结论。未修改TCP或监控产品代码，未作性能优化、部署、浏览器验收或AI调用；功能通过与性能目标未通过分别记录。Git发布按随后用户授权执行，不纳入原始日志、凭据、依赖或本机配置。
