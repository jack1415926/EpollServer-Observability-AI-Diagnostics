# T11/T12：证据计划、局部支持检查与复现记录

状态修正（2026-10-28）：**T11/T12部分完成，不能标为整项完成。** 已交付的工程能力和通过检查仍有效，但已知语义与生成格式问题尚未全部解决，具体待办和关闭条件见[RAG待办清单](RAG_DIAGNOSIS_TODO.md#t11t12未解决问题与关闭条件)。

2026-10-27～28的实现涵盖最小必要契约、观测/故障回答分流、支持定位、失败保真、源审阅入口及版本/用量记录。以下保留已做工作与原始验收范围，不能用它们替代未解决问题的关闭。程序校验不是自然语言逻辑证明，也没有诊断质量达标结论。

## T11：按问题补齐解释所需证据

先用完整脱敏问题分类，读取本题已选运行/历史来源；再根据问题与已取数据补充六类固定契约：

| 契约 | 用途 |
|---|---|
| sampling | 文件采样、fresh/stale、null、有效部分与完整总量、业务队列语义 |
| worker_states | dead、process_unavailable、身份不符与快照字段不符；停止状态不等于退出 |
| worker_policy | 名单非健康检查；Worker意外退出整体停止，不自动补起 |
| events | 历史事件、当前detection摘要、active与恢复证据、实例与时点 |
| experiments | 明确基准/候选、历史记录、条件可比性、缺失均值与零基准 |
| capabilities | 尚未支持的指标、未主动探测业务、日志/用户陈述的未核验范围 |

`diagnosis/selection.py`依据已选来源与问题/异常状态决定主题；`observation_contracts.json`保存简短条款及源码/文档引用哈希。读取时核对引用版本，变更或不可读时返回明确不可用；必要契约不可用则不生成可能原因/排除项，仅报告观测和未知。它是程序内部固定资料查表，不接受用户任意路径，不向模型开放Shell/SQL或新增运行工具，不读取未选运行端点。代码树契约不等于已经独立核验目标运行二进制版本；部署版本仍须核对。

保留原七种工具与白名单派发。普通有效计数/监控存活请求使用观测回答，不跑多余Agent选工具循环、不强填根因或未知；计数按当前名单expected/valid完整性说明覆盖范围，不虚构名单外Worker。知识只查资料，实验只读历史，范围外/澄清无证据读取。明确运行态读取误分知识可窄范围纠偏，否定“只解释、不读取当前”优先保留；无效分类不回落成全量读取。实验比较核对原文UUID集合，并对可明确核验的基准/候选前缀或无角色时的原文顺序进行绑定，不擅自选择最近两条。

相同工具参数在本任务复用，取证时间不续期；共享索引只共享只读数据，provider、检索轨迹与缓存按任务隔离。报告表达来源时点，不把生成时刻当成样本/事件发生时刻，也不证明报告返回后状态仍相同。诊断器不执行建议。

故障Analysis区分facts、excluded_causes、possible_causes、unknowns、next_steps。excluded是能由观测/契约排除的解释或错误推断，不代表所有曾经发生的信号均已排除。仍只允许insufficient_evidence/possible_causes；确认根因须另有同实例同时间的直接因果证据与区分性验证，当前版本不输出“已确认根因”。普通观测的conclusion=observation仅表示读回数据，不是根因确认。

## T12：能够机器核对什么

外层schema_version=1、JSON存储不变，analysis_schema_version=2。每条事实、候选和排除项声明basis与1～5个support：

```json
{
  "text": "Worker连接数7",
  "basis": "observation",
  "evidence_ids": ["E1"],
  "support": [{"evidence_id":"E1", "pointer":"/response/data/workers/0/metrics/connections_current",
               "value":7, "quote":"", "operation":"value"}]
}
```

- value：RFC6901指针相对于该E编号excerpt解析后的JSON根，核对原始标量及类型；布尔值不能冒充数值。
- length：指向集合并核对整数数量，空列表不能以null表示；资料、日志和用户陈述也可用原样短摘录，不能用释义冒充引用。空白摘录不能绕过定位。
- evidence_ids必须与support引用集合一致；未知编号、不可用证据、错误指针、截断而不可解析的JSON、不匹配值/数量/摘录会拒绝。格式和支持问题共享最多一次最终修正，不重取证、不重试传输错误。
- 检查声明的时间/PID/标识原值，拒绝跨Worker定位混用；对部分数字、完整UUID/哈希、null填0、来源范围和少量已知项目矛盾做有界检查。数值上下文包含同一定位的祖先标量，文档阶段号/手册节号经来源核对后作为来源标签；不将这些标签当成实测值。
- 历史记录正文不能冒充当前观测；本次查询状态、数量及detection摘要另按控制面范围处理。联合观测与契约可支持排除解释，但不把未核验用户/日志自动升级为事实。用户陈述单独成E项，不借health证明用户说过什么。

这些检查保证的是**声明的局部定位和一致性**，不能证明每个自然语词的含义、所有数字的字段归属、任意代词/因果关系、单位转换或所有可能措辞。完整标识匹配不等于已验证运行身份；scope标签正确也不保证正文没有混淆来源。`support_validation.semantic_status=requires_review`始终保留。字段约束/引用存在性/程序通过不等于根因正确，下一节的部分支持与失败就是反例。

成功/失败记录均保存提示词版本/哈希、代码哈希与依赖版本、加载的索引/策略哈希、实际使用契约哈希、模型原分类与执行计划、BM25/Dense/RRF/重排过程及分数/缺失、实际返回model/usage/finish_reason/Responses状态或缺失。原生Responses未返回Chat finish_reason时为null，不伪造stop；别名不等于不可变权重版本。

失败任务仍是failed/timed_out/interrupted，保存kind=failure、delivery_status=rejected、有限证据和调用元数据，不把无效生成结果放成成功事实。生产失败记录的excerpt上限更小，不能保证恢复未保存的完整输入；开发评测另保留原始脱敏候选、来源/代码/提示词/数据/索引快照。JSON字符串在统一脱敏层递归处理，重复脱敏保持结构、原值定位和已有证据哈希。

## 固定对照与源审阅

`evaluation/evidence_v1`在运行前冻结12题及接口回放：3道原失败开发题、3道知识/计数/范围控制题、6道新家族保留题（事件、能力缺口、用户陈述、实验比较、日志指令、快照身份）。工具数据都是合成回放，不是真实故障；标签不进入模型或索引。题集由项目助手编写，保留题生成结果未用于反复调提示词，不称外部盲测。代码/索引/提示词及来源快照在各运行目录中，远端模型仍非确定性，不能承诺逐字复现。

固定组在分类后对运行题预读五种回放来源、全部契约与一次通用检索；按需组使用当前策略。两组同一运行使用同实现/提示词/约束，但模型分类/生成独立、先按需后固定，存在顺序和远端波动。没有通过扩大生产权限实现固定组。

| 运行 | 原调用/后端通过/拒绝 | 说明 |
|---|---|---|
| 最小依赖题 | 1 / 1 / 0 | 能力预检，含一次最终修正，不计新题集质量 |
| 首轮开发 | 10 / 4 / 6 | 暴露空列表表达、来源范围及数字标签误拒；未覆盖旧结果 |
| 第二轮开发 | 10 / 7 / 3 | 一次中断目录无首题落盘结果；表中是另一个恢复目录 |
| 最后开发生成对照 | 10 / 8 / 2 | 固定D3仍有未绑定具体值/范围问题；知识C2来源节号误拒另行修正 |
| 冻结保留集 | 8 / 6 / 2 | 6道按需题加2道固定组：实验题摘录非原文；快照身份题在摘录模式仍重复填写非null的value，违反定位互斥规则，均拒绝 |
| 知识控制单题复测 | 1 / 1 / 0 | 来源编号修正后的独立新调用，不能回填开发对照原成绩 |

原始目录为 `artifacts/evidence-t11-t12/{smoke,dev,dev-v2,dev-v2-resumed,dev-v3,holdout,knowledge-control}`。中断目录无可核对的完整调用记录，在途请求是否计费未知，不填0，不计为通过。失败与最终修正请求均纳入各自用量；不把不同版本/目录合成一轮漂亮分数。

**只比较同题成对部分**（含拒绝的成本；工具数不含内部契约查表）：

| 同题对照 | 按需：工具执行/API请求/可得DeepSeek总tokens | 固定：工具执行/API请求/可得DeepSeek总tokens |
|---|---|---|
| 开发4对 | 6 / 15 / 86,983 | 27 / 30 / 215,931 |
| 保留2对 | 5 / 9 / 59,752 | 12 / 12 / 88,927 |

开发配对全尝试耗时中位数22.839/35.898秒，保留配对23.329/24.882秒。它们只描述本组观测，不是独立冷启动、统计显著因果或可承诺的节省比例，更不是TCP/监控正式性能开销。Rerank未返回usage时保持缺失；各API的input/prompt/output/completion/total分别原样汇总，不相加重复计费，不估算金额。

已对最后开发生成对照、保留集和控制复测共15份后端通过报告逐条核对事实、假设、排除项、summary及建议。评阅由项目助手按源码、资料和fixture执行，**不是外部人工专家或独立盲评，也不是把另一模型自动评分当真值**。结果8份通过、6份部分支持、1份失败；失败原调用另外统计，不进入成功报告语义分母。这不是诊断准确率，包含观测、知识和范围控制题。

残余包括：T与t措辞/时点不精确；把采集不可用称为服务不可用；固定组仍把单纯暂停列为dead候选；历史阈值不能直接当当前配置；事实/假设分组；远端测量地点不等于被测目标不同；事件历史与当前detection摘要混写。原修复目标中的文件采样、暂停/退出区分及不自动补起已进入必要契约，但这些更细问题仍需后续独立问题和外部复核，不宣称模型语义已全面解决。

评阅文件及逐claim标签为 `evaluation/evidence_v1/reviews-*.json`，结果/报告哈希和条目数绑定由 `diagnosis.review_evidence`核验；摘要为 `summary-*.json`。复核者可另起修订、说明理由，不能静默改原测量或拿已评阅题冒充新保留集。支持假设只表示适合列为明确未验证方向，不是根因被证实。

## 实际工程与资源验收

63项合并回归在允许asyncio本地唤醒的沙箱外通过，完整日志为 `final-unit.log`；含原任务服务、失败元数据持久化（内存Store）、值/数量/类型/时间/标识/null/范围负控、源审阅绑定、幂等脱敏等。旧实现最小失败与嵌套JSON失败分别保存在 `red-tests.log`、`red-redaction.log`。Node运行生产渲染函数覆盖新/旧报告、排除项、用户陈述、失败及取证时观测；JS/Python语法、Make HTTP与diff检查通过。没有宣称完整浏览器/可访问性复测。

私有MySQL预检目录 `artifacts/stage3-live-lpktdfoz` 已通过启动、管理员SHUTDOWN、退出和目录删除。真实链路 `artifacts/stage5-live-uia7bqxg` 完成五类HTTP/Python/模型任务、精确SQL读回和重启保留；另用真实401拒绝验证失败元数据落库/重启及监控liveness。此为smoke，未启动TCP，不算新Worker暂停/恢复或完整故障隔离。

脱敏收尾补丁后，`artifacts/stage5-live-ng4nkhka`复用已生成报告，无新模型调用，验证嵌套敏感字符串SQL样例、原报告读回、重启和清理。已测成功报告经新脱敏/当前检查器离线核对未改内容且仍通过；先前真实链路与后续小补丁保持各自哈希，不伪称重做所有模型生成。默认数据时点展示补充不改变旧报告正文。两个私有实例及测试HTTP/Python均确认退出后删除数据；原服务身份在脚本中核验保留，未操作业务数据或本机nginx.conf。

## 索引与交付边界

本批评测使用T5的305片段索引，SHA-256 `91af1bd8050d2f330894b1152caa33e639df551ded3e7d53103c642156cd4fb8`，另存before-index与每轮index/source快照。交付文档追加后会另行增量重建当前索引；其最终哈希在 [验收清单](../evaluation/evidence_v1/acceptance.json)记录，不用新库改写上述成绩。T4、T5、v1冻结索引保留，47.92%锚点评分未改。

当前索引已完成原子安装：25份来源、308片段，复用299个向量、新建9个；SHA-256 `7ec14185a71955a72f809169cdcb5d86f1d4a93c65fb42f1c021c28ffc25b030`。当前Retriever与documents()/策略一致；该交付索引没有再被称作上述旧305片段测量。评测复盘不在普通白名单，问题记录中的评测进度标为planning_progress，避免把已评阅结果默认当成独立故障知识。

已实现能力与通过检查保留，T11/T12整项仍未完成；必须按待办清单关闭已知问题并保留复核证据，再重新判断完成状态。没有新运行插桩、TCP性能负载、正式监控开销对照、正式库初始化、部署或推送；已有非测试服务未重启，使用新代码/索引需另行重启。T6～T10、更多故障/新独立质量集、专家复核与正式部署验收也仍是后续工作。

```bash
# 工程回归：服务测试须允许asyncio本地唤醒，使用有界执行
timeout --signal=TERM --kill-after=2s 45s .venv/bin/python -m unittest \
  testscript.test_diagnosis testscript.test_diagnosis_evidence testscript.test_corpus_layers \
  testscript.test_diagnosis_evaluation testscript.test_diagnosis_structured \
  testscript.test_diagnosis_responses testscript.test_diagnosis_routing -v
node testscript/test_diagnosis_render.js
# 有费用：固定回放、真实模型；始终用新的输出目录
.venv/bin/python -m diagnosis.evaluate_evidence --split dev --compare --output artifacts/evidence-dev-new
# 无模型调用：核验评阅哈希/条目数并重算，输出也用新文件
.venv/bin/python -m diagnosis.review_evidence artifacts/evidence-t11-t12/holdout \
  --reviews evaluation/evidence_v1/reviews-holdout.json --output artifacts/evidence-review-new.json
```
