# 监控吞吐差异的分项探索

2026-10-31。用户授权继续定位[正式开销评估](MONITOR_OVERHEAD.md)中的吞吐差异。原九轮full组QPS中位数下降13.70%的记录保留，本页是新探索，不回填原成绩或直接判定新的正式验收通过。

## 本批边界与反馈信号

TCP与Drogon二进制均与10月30日正式测量哈希一致，不修改产品实现。保持4 Worker × 120业务线程、MySQL池16、TCP/Drogon CPU 0–3、客户端CPU 4–11、200并发/6进程、每连接10000次Ping、原Lua和延迟抽样参数。每条件正式200万次、预热10万次，使用独立Redis和私有监控MySQL，不操作原业务数据。

反馈信号为当前批次不同条件的QPS/P95及进程资源、功能状态、请求失败与清理状态。保持原负载时每轮约一分钟；秒级smoke只验证控制与生命周期，不作为吞吐差异的可靠复现。先跑四条件，再仅按结果复核必要条件；每轮保留，不能采样直到凑成期望结论。

| 条件 | 快照 | Drogon采集/CPU/RSS | 监控数据库及事件 | 负载期HTTP轮询 |
|---|---|---|---|---|
| snapshot | 开启 | 不启动 | 无监控连接，私有库仍在运行 | 无 |
| collector | 开启 | 开启 | 移除全部监控DB环境变量，事件接口必须503/event_store_disabled | 无 |
| events | 开启 | 开启 | 私有库连接，检测running、存储ok | 无 |
| full | 开启 | 开启 | 私有库连接，检测running、存储ok | 每2秒读取overview/workers/events各一次 |

无轮询条件在负载前后分别读取workers/events验证状态，负载期间不调用它们。所有探索条件均有相同2秒周期的进程资源观察线程，固定CPU 4–11；记录私有监控MySQL，以及存在时的Drogon资源。完整组的HTTP轮询也在此线程执行，启动/停止及资源观察成本不能误称纯HTTP接口CPU成本。

私有监控MySQL在所有条件中保持运行，collector只禁止Drogon连接，并非关闭该数据库实例。events与full的真实报告写入/读回预检在正式负载前完成，不混入负载。数据库/事件档同时增加SQL客户端和检测/持久化线程，仅凭该档差异不能再细分其中哪部分是原因。

## 检验方向

1. 若后台采集/Drogon是主要来源，snapshot→collector应出现差异。
2. 若数据库/事件是主要来源，collector→events应出现差异。
3. 若轮询是主要来源，events→full应出现差异。
4. 若时间波动是主要来源，反向或相邻复测时差异方向可能变化。

单轮变化只用于缩小范围。先确认重复性，再讨论原因；没有区分证据时保留未知，不凭低平均CPU或一次改善断言根因。

## 运行与证据

复用原脚本及已有真实报告，不增加服务端开关或新依赖：

```bash
PYTHONPATH=testscript python3 -m unittest test_monitor_overhead test_process_group test_stage3_lifecycle -v
# 仅控制/生命周期预检：每条件正式2万次、预热1万次
python3 testscript/benchmark_monitor_overhead.py --smoke \
  --explore snapshot collector events full --report /absolute/path/to/real-report.json
# 第一组四条件探索，默认原200万次/轮
python3 testscript/benchmark_monitor_overhead.py \
  --explore snapshot collector events full --report /absolute/path/to/real-report.json
```

`--explore`要求显式列出条件顺序，后续可用于反向/相邻复核；探索输出没有within_target或正式验收结论。原不带该参数的九轮入口和阈值汇总保留。

新目录为`artifacts/monitor-overhead-exploratory_{smoke,decomposition}-*/`，沿用manifest、owned-services、每轮context、客户端/资源原始结果及cleanup记录。新增`monitor-before.json`和`monitor-after.json`记录负载前后监控状态；`http-samples.json`的无轮询组responses为空，full保存实际轮询。summary带探索标记，旧目录/索引/评测不覆盖。

## 当前验证

21项针对性回归通过（8项开销控制、5项进程组、8项数据库生命周期）。四条件短预检共12万次Ping零失败，collector实际503禁用、events/full实际检测和存储正常，无轮询组没有负载期HTTP请求。进程、端口和配置清理通过，本机配置哈希不变。短预检原始目录为`artifacts/monitor-overhead-exploratory_smoke-9mv39w9m/`。

第一组四条件及随后collector→snapshot反向复核均已完成，共1200万次正式请求、60万次预热零失败；另有上述12万次短预检。两组原始目录分别为`artifacts/monitor-overhead-exploratory_decomposition-3oraf1m6/`和`artifacts/monitor-overhead-exploratory_decomposition-x4_y1t8g/`。每轮各2万条延迟样本；摘要及证据哈希见[本批验收记录](../evaluation/monitor_overhead_diagnosis_20261031/acceptance.json)。

| 批次 / 条件 | QPS | 抽样P95 ms | 四Worker观测连接峰值（降序） |
|---|---:|---:|---|
| 第一组 snapshot | 37087.271 | 8.959 | 54 / 51 / 49 / 46 |
| 第一组 collector | 29337.237 | 10.017 | 65 / 47 / 45 / 43 |
| 第一组 events | 31759.016 | 9.643 | 63 / 56 / 46 / 35 |
| 第一组 full | 37023.249 | 9.052 | 53 / 52 / 50 / 45 |
| 反向复核 collector | 32077.588 | 9.402 | 61 / 50 / 45 / 44 |
| 反向复核 snapshot | 32408.300 | 9.320 | 61 / 51 / 45 / 43 |

第一组collector相对snapshot下降20.90%，但反向复核仅下降1.02%；第一组full相对snapshot仅下降0.17%。这组数据没有复现“监控功能逐档增加导致吞吐逐档下降”，也不能从一次小幅变化宣称开销为零或已经正式达标。

## 已确认的测量缺口：Worker连接负载未控制

本批复核了新六轮与原正式九轮的已有server.log，只使用context中四名Worker的PID，限定在各轮资源采样器的测量时间窗内，按PID提取“当前在线人数/总人数”中的连接数（该字段是连接数量，不是登录人数）。日志墙钟按本工作区Asia/Shanghai解释。每Worker取窗口内观测峰值；它们是周期日志，不是跨Worker同瞬时快照。15轮峰值之和均为200，不能据此声称已采集逐Worker累计请求数。

原九轮两个最慢full轮的峰值为67/51/41/41和67/46/44/43，而首轮off为55/53/46/46。新第一组较慢collector最重Worker承接65条连接，其平均CPU约93.50%，其余Worker约50.87%～56.64%；较均匀的full组为53/52/50/45，Worker平均CPU约66.81%～81.06%。反向复核两组最重Worker均为61条，QPS也接近。

已确认的是：**配置中的总并发相同，并没有控制实际逐Worker连接分配。** 客户端每连接固定请求数，负载不均可能影响整轮完成时间；当前证据只支持它是必须控制的混杂因素，不能确认它解释了全部13.70%差值，也不证明采集、SQL或轮询没有开销。没有足够证据时不修改产品代码、不把相关性写成确认根因。

复核可离线重跑，传入任一原始批次目录；不用启动任何服务：

```bash
python3 - /absolute/path/to/experiment-root <<'PY'
import collections, datetime, json, re, sys
from pathlib import Path
zone = datetime.timezone(datetime.timedelta(hours=8))
pattern = re.compile(r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}).*? (\d+): .*?当前在线人数/总人数\((\d+)/(\d+)\)')
for path in sorted(Path(sys.argv[1]).glob('*/measured-resources.json')):
    resources = json.loads(path.read_text())
    context = json.loads(path.with_name('context.json').read_text())
    ids = {worker['pid'] for worker in context['workers']}
    start = datetime.datetime.fromisoformat(resources['started_utc']).timestamp()
    values = collections.defaultdict(list)
    for line in path.with_name('server.log').read_text().splitlines():
        match = pattern.search(line)
        if not match:
            continue
        stamp = datetime.datetime.strptime(match[1], '%Y/%m/%d %H:%M:%S').replace(tzinfo=zone).timestamp()
        pid = int(match[2])
        if pid in ids and start <= stamp <= start + resources['duration_sec']:
            values[pid].append(int(match[3]))
    assert set(values) == ids
    peaks = sorted((max(items) for items in values.values()), reverse=True)
    assert sum(peaks) == 200
    print(path.parent.name, peaks)
PY
```

这里的峰值断言用于核验本批日志摘取，不是把“恰好相加200”定义成通用正确性标准；其他短负载或未遇到周期日志的批次不适用。

## 下一步及交付边界

首轮分项探索完成，稳定瓶颈和因果根因尚未确认。下一步应先控制或准确记录每个Worker的连接/请求负载，再比较开关条件；例如在测量前建立并核验连接归属与配额，而不是只固定总并发。若改变连接建立方式或计时边界，必须作为新的实验方法对全部比较条件一致应用，不能把新数字混入旧九轮或称为产品优化收益。

同日后续已交付[均衡压测原型及四轮尝试](BALANCED_WORKER_BENCHMARK.md)：各Worker50条连接、各50万次测量请求，off/full各两轮，full的QPS中位数下降1.97%、P95上升1.24%。它使用新的预连接/同连接预热/屏障计时方法，本页六轮及旧九轮保持原样，不能直接合并或算作已经确认原差异的全部原因。

本批无产品代码修复，也未取得新的正式开销达标结论。原九轮结果及其证据哈希均未改变。所有短测和探索进程启动身份、端口、临时配置和私有数据库数据目录均已独立核验退出/释放/清理；原业务MySQL PID2305仍在且3306可达，本机nginx.conf哈希不变。未启动付费模型、重建索引、部署、提交或推送。
