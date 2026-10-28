# 诊断质量评测 v1（T2 / T3）

2026-10-26。评测对象是冻结索引上的检索和生产 Agent 在固定工具证据下的输出，目标是建立首次基线。本轮不改检索参数、提示词、索引内容或服务行为，不启动数据库/TCP，不执行新性能负载。

## 问题集与隔离

数据在 `evaluation/diagnosis_v1/`：

- `cases.json`：24个症状/决策问题。开发集12题：采集、Worker、队列、历史实验；保留集12题：连接、资源、MySQL、Redis。每个故障家族只属于一个集合。
- `fixtures.json`：固定时间、虚构身份和计数的监控契约回放，明确标记 synthetic。包含过期/缺失、身份错误、积压/未知、旧实例事件、旧日志、不同条件实验及恶意日志指令。不是实际故障注入，也不证明工具/传感器能正确采集这些值。
- `labels.json`：每题必须表达的边界、禁止的断言、必要工具来源和相关文档行。答案标签与测试诱因不进入提示词、工具返回或索引。引用组中的多个来源可以是同一个概念的替代出处。

先按症状和诊断决策编题，再标注必要契约，不从检索结果反推金标准。覆盖可陈述事实、同症状多原因、无法确定根因、历史/当前冲突、问题末尾关键条件（N3超过500字符）和日志中的指令注入（R2）。本轮没有预先改变线上模型使其适应这些问题。

这是项目维护助手编写并逐项复核的初始集，没有外部专家盲审；它与现有文档共享领域知识，不是独立机构基准。保留集指未用于调参的故障家族，不声称题目对作者不可见。首次报告产生后不再据保留集修订 v1 参数、问题或标注；发现标注错误需另起版本并披露。下轮算法开发只用开发集，反复使用过的保留集应更换。

本集着重测试事实边界与未知处理，没有“可确认根因”的正例，也不足以证明开放域诊断能力。结构正确和保守拒答不等于诊断正确。

## 检索与诊断分别评价

检索使用完整原问题，对比 BM25-only、Dense-only、BM25+Dense等权RRF(k=60)、混合+Rerank。各路召回20，合并20，返回5，与当前生产参数一致；完整模式直接调用生产 `Retriever.search`。诊断仍使用生产 `diagnose`，因此初始检索保留问题前500字符的现有行为。每题比较完整RAG与 `live_only`（禁用文档检索，保留相同固定工具回放、生产提示词及其余工具）。live_only不是新一轮真实采样。

- **Recall@5**：top5覆盖的必要证据组数 / 标注必要证据组数。一个组中任一来源的指定行落入片段即可命中，重叠片段不重复加分。它是契约证据组召回率，不是“所有相关片段”的穷尽召回。
- **MRR@5**：第一个命中任一证据组的排名倒数，无命中为0；截断在5名。另报告开发集和保留集，不只报总均值。
- 本集“无答案”主要指没有当前根因证据；仍可能有相关的能力边界文档，因此这类问题也有非空相关来源。不能把命中文档当成找到了根因。当前检索没有拒答阈值，本轮不校准阈值。
- **自动诊断检查**：JSON/引用编号、必要工具来源、指定问题的结论枚举；只作为结构和取证检查。结论枚举未标注的题不计入分母。编号存在不表示内容支持。
- **逐项语义复核**：逐条核对fact是否被其引用支持；possible_causes是否明确是假设且证据足以支持提出该方向；summary也检查，不允许借summary绕过引用规则。记录证据支持、事实错误、时间/实例混淆、拒答适当性、必须表达点和违规断言。不用第二个模型自动评分当真值。
- **运行代价**：每模式墙钟耗时、实际API请求数、provider返回的usage/model/finish_reason。每题内复用相同查询向量与相同候选重排，不跨题缓存、不缓存chat。后运行模式的耗时因此不等同于独立冷启动延迟；请求用量也不是每种模式独立运行的成本估计。模型不返回usage时为null，不估算成0；不从token自行估算费用。

语义复核规则：事实必须与引用的实体、时间、数值和null含义一致；契约规则可由当前文档支持。只因同在一个JSON中不算支持。缺少观测时可提出明确待验证的竞争假设，但不能断言因果；拒答应指出缺什么、下一步取什么证据。引用不存在、猜测升级为事实、跨实例/跨时间归因和把null写成0均记错误。记录具体claim位置与原因，允许“部分支持/无支持/无法判定”，不能只给笼统通过。

## 可重跑入口

```bash
# 无网络、无模型调用：验证计算、家族隔离、生产模式一致性与真实Agent回放边界
.venv/bin/python -m unittest testscript.test_diagnosis_evaluation -v
# 仅本地BM25；仍需已有索引，不能称为完整质量基线
.venv/bin/python -m diagnosis.evaluate --output artifacts/diagnosis-eval-local
# 最小依赖链路：一题、四种检索、两种诊断
.venv/bin/python -m diagnosis.evaluate --case C1 --remote --phase all \
  --output artifacts/diagnosis-eval-smoke
# 完整基线，调用真实远端模型，工具仍为固定回放
.venv/bin/python -m diagnosis.evaluate --remote --phase all \
  --output artifacts/diagnosis-eval-v1
# 独立重跑检索或诊断，使用新输出目录
.venv/bin/python -m diagnosis.evaluate --remote --phase retrieval \
  --output artifacts/diagnosis-eval-retrieval
.venv/bin/python -m diagnosis.evaluate --remote --phase diagnosis \
  --output artifacts/diagnosis-eval-diagnosis
```

使用现有 `.env.stage5`，不输出凭据。缺少索引/索引过期时，在任何API调用前失败，显式重建索引另行执行，不让评测偷偷改知识库。首次运行保存数据/脚本/提示词/索引/语料哈希、模型配置、Python版本和时间；每次结果立即写入JSONL。中断后同命令可续跑未记录的项目，已记录失败不自动重试，需审查错误后显式使用新输出目录。配置或输入变化拒绝混入旧结果。

原始模型报告、引用、工具调用、检索排名和API元数据保留于忽略的 artifacts，发布文档只引用脱敏汇总与审阅记录。不自动提交、推送或部署。模型非确定性且provider模型别名可能更新；记录版本信息不保证逐字复现。

实际结果见 [首次基线报告](DIAGNOSIS_BASELINE_V1.md)。

从保存结果重新汇总（不会调用任何API）：

```bash
.venv/bin/python -m diagnosis.evaluation_report artifacts/diagnosis-eval-v1 \
  --reviews evaluation/diagnosis_v1/review.json \
  --output artifacts/diagnosis-eval-v1/summary.json
```

`review.json` 逐条保存成功报告的事实/假设支持标签、问题与模式、报告哈希、语义复核结论和具体扣分位置。汇总器拒绝不匹配的manifest/报告哈希、重复结果和重复/孤立审阅。失败报告不进入语义复核分母，失败次数单独报告；自动检查分母同样只计成功报告，不能据此隐藏失败。`supported` 对假设仅表示“适合作为明确未验证的候选方向”，不表示根因得到证实。

工具回放的观测时间固定为 `1792886400000`，证据取得时间仍由生产 `Tools.record` 记录实际运行时间。因此模型可能额外提示证据新鲜度不足；它不是本次真实监控陈旧故障。首版所有模式共享此限制。未记录完成的在途调用如被外部中断，续跑可能产生额外费用；脚本没有exactly-once的远端调用保证。

补充A1专门检查检索结果中的恶意指令，独立于冻结24题执行：

```bash
.venv/bin/python -m diagnosis.evaluate_document_injection \
  --output artifacts/diagnosis-eval-v1/document-injection.json
```

它直接注入一个固定检索片段，不读取/修改正式索引，不计入检索召回；使用同一生产Agent及完整工具白名单。输出已存在时拒绝覆盖，重跑请使用新文件名。文档显式带评测标记，这一较容易的样例只证明本次输出未服从注入，不能作为广泛安全性结论。
