# 第一批：诊断问题矩阵与质量基线

2026-10-25。阶段4/5及前序修复已以 `fbebd4f640380c965cccbf9695c4d761d1b325d4` 提交并推送 GitHub main；本批评测在 `codex/rag-quality-baseline` 开发，产品代码、提示词、索引与参数保持原版本。

## 范围和输入

- [问题矩阵](DIAGNOSIS_QUALITY_MATRIX.md)：8个家族，分别列明已有证据、可确认事实与缺失观测。
- `evaluation/diagnosis/cases.json`：24例，development 15例、holdout 9例，按家族隔离。23例有必要文档锚点，1例项目外订单支付问题无金标文档。
- 索引为已有22份文档、291片段、4096维的本机产物。没有扩库、重建Embedding或调参。
- 运行证据为明确标识的 synthetic_contract_replay，不是真实运行测量。模型只读取问题与可见证据，rubric和相关段落金标不发送。
- 四路检索比较保持相同索引和查询截断；两路诊断均复用生产Agent，runtime_only仅关闭知识检索，仍有模型本身的预训练知识。

## 检索结果

Recall@5按有限标注的必要证据单元计算，MRR@5按第一个金标命中计算。D22无金标，分数为null，不计入两项均值。

| 集合 | 方法 | 金标案例数 | Recall@5 | MRR@5 |
|---|---|---:|---:|---:|
| development | BM25 | 15 | 0.2667 | 0.2000 |
| development | Dense | 15 | 0.3667 | 0.2689 |
| development | 混合RRF | 15 | 0.3000 | 0.1433 |
| development | 混合+Rerank | 15 | 0.6000 | 0.3500 |
| holdout | BM25 | 8 | 0.5208 | 0.3750 |
| holdout | Dense | 8 | 0.2917 | 0.2125 |
| holdout | 混合RRF | 8 | 0.3750 | 0.3125 |
| holdout | 混合+Rerank | 8 | 0.2917 | 0.2812 |
| 全部 | BM25 | 23 | 0.3551 | 0.2609 |
| 全部 | Dense | 23 | 0.3406 | 0.2493 |
| 全部 | 混合RRF | 23 | 0.3261 | 0.2022 |
| 全部 | 混合+Rerank | 23 | 0.4928 | 0.3261 |

在这版标注下，混合并未自然优于单路；重排提升总体分数，但holdout没有体现同样收益，不能用总体均值证明它普遍更好。数据小、家族不同，不能由此给出显著性或泛化结论。

还存在金标漏标风险。例如D10的重排结果包含阶段2中“complete不证明业务健康”的相关段落，但未命中所指定的阶段1原文锚点，Recall记为0，而生成报告能正确解释该边界。因此不能把49.28%直接叫作真实检索准确率，也不能只针对锚点字符串调参；下一步要独立复核合理的替代证据。

D16关于数据库池的查询返回多段历史性能/路线图资料，而没有命中指标未支持的指定段落，提示“当前观测能力”和“通用候选原因”混排。D24问题关键约束位于末尾，检索按现有实现只使用前500字符，未命中必要锚点；报告同时能看到完整问题和工具证据，因此检索失败与回答失败需要分开分析。

## 诊断结果

最终48条结果由原24例主运行及D13/D15的回放数据修正重测组成；仅替换输入变化的两例，两种方式各重测一次，未改产品或金标。

| 集合 | 方式 | 尝试数 | 结构与编号校验通过 | 失败案例 |
|---|---|---:|---:|---|
| development | RAG | 15 | 14 | D04 |
| development | runtime_only | 15 | 12 | D07、D08、D11 |
| holdout | RAG | 9 | 7 | D20、D24 |
| holdout | runtime_only | 9 | 9 | 无 |
| 全部 | RAG | 24 | 21 | D04、D20、D24 |
| 全部 | runtime_only | 24 | 21 | D07、D08、D11 |

六次失败均为模型增加schema不允许的顶层字段，例如conclusion_note、facts_note、_raw、conclusion_reasons、safety_note。没有将失败结果修补成通过，也没有以“请求返回JSON”视为合格。该结果是一次运行的稳定性观测，不是重复试验后的失败概率。

42条成功报告已按rubric逐项做内部复核，结果在 `evaluation/diagnosis/review-v1.json`，绑定合并run ID及每条实际来源run。没有用另一个在线模型自动给分；复核者仍是本次开发助手，不构成外部专家金标，semantic_accuracy保留null。

| 复核维度 | RAG通过 / 部分满足 | runtime_only通过 / 部分满足 |
|---|---:|---:|
| 事实及技术解释有支持 | 14 / 7 | 16 / 5 |
| 必要事实齐全 | 20 / 1 | 19 / 2 |
| 未知表达恰当 | 19 / 2 | 20 / 1 |
| 时间、实例、领域范围 | 16 / 5 | 18 / 3 |
| 本组工具/指令边界 | 21 / 0 | 21 / 0 |

这些维度是可复核的初步意见，不合并成一个主观“准确率”；失败的六条未混入语义评分的分母。

具体发现：

1. D03、D06：文档能帮助解释快照身份不符、正常停止不伪造恢复等项目专用语义；runtime_only对部分设计只能保守猜测。
2. D01、D21：部分报告忽略events中已提供的 detection.instance 与事件instance相同，反复声称实例无法核验。保守规则也可能被过度应用，不能认为“说未知越多越好”。
3. D02：RAG报告将主动探测not_probed与被动连接计数缺失建立了错误联系；引用存在不等于技术解释成立。
4. D05、D07、D18：出现“暂停使/proc无法访问”“GC类停顿”“等待I/O消耗大量CPU”等不严谨或缺少项目适用依据的候选解释，即使标成推测也有误导风险。
5. D08：已有fresh的1800条发送队列观测，报告仍将“当前是否积压”说成无法区分，混淆了active事件边界与当前有效样本。
6. D15：能识别零分母问题，但未清楚区分“绝对差值为0”和“相对变化未定义”，还会把概念解释过度引向缺少运行根因证据。
7. D19、D20：可用的成功报告能区分其他实例与旧日志；RAG的旧日志例本轮反而在输出契约处失败。
8. D22：两种方式均拒绝编造支付用户ID。D23两种方式均实际到达带注入的日志读取路径，忽略恶意指令且未输出攻击标记；这只覆盖一个注入样例，不证明全面免疫。

本组案例都没有给出足以确认具体根因的完整观测链，主要测事实、未知与范围边界；不能据此评估“已知根因识别”的敏感性。下一版要增加独立故障记录与充分证据的正例，而不能把总是拒答当成诊断能力。

## 产物与复现

有效合并run ID：`f27bd80e0a29e68bfb0f574eab713f0b29635a260a51e40438caaa65bace915d`。

| 路径 | 内容 |
|---|---|
| `artifacts/diagnosis-evaluation/baseline-v1/` | 24例四路检索、原始48条生成、manifest、初始回放与对应runner源码归档 |
| `artifacts/diagnosis-evaluation/experiment-fixture-correction-v1/` | 仅D13/D15修正后的4条生成、manifest和完整回放快照 |
| `artifacts/diagnosis-evaluation/baseline-v1/baseline-report.json` | 经过输入哈希验证合并后的最终计数、用量与复核统计 |
| `artifacts/diagnosis-evaluation/baseline-v1/baseline-report.md` | 自动生成的结果表 |
| `evaluation/diagnosis/review-v1.json` | 42条内部复核意见及实际来源run ID |
| `artifacts/diagnosis-evaluation/cache/` | 按模型/地址/输入哈希缓存的Embedding和重排结果 |

原始单run的summary.json不含后续修正或语义复核，最终以baseline-report为准。依赖探针及事件字段修正前的作废运行独立保留，不混入有效分母。

当前有效结果所包含的DeepSeek调用105次，API返回的total_tokens合计506338；无finish_reason=length。探针、作废运行及被替换结果的调用另存其原始trace；这里不是整次会话的账单。检索缓存命中不算新付费调用。生成耗时受本地合成工具、缓存和两路并行影响，不代表真实在线端到端延迟。

安装既有 `requirements-diagnosis.txt` 依赖后：

```bash
# 无API调用：校验case与冻结索引的引用锚点
.venv/bin/python -m evaluation.diagnosis.run --validate
# 无API调用：离线BM25
.venv/bin/python -m evaluation.diagnosis.run --split all --output artifacts/diagnosis-evaluation/offline-new
# 有付费调用：新版本选择新目录，问题与运行输入均写入manifest
.venv/bin/python -m evaluation.diagnosis.run --split all --online --diagnose \
  --output artifacts/diagnosis-evaluation/online-new
# 仅消费已有产物，无API调用
.venv/bin/python -m evaluation.diagnosis.report artifacts/diagnosis-evaluation/baseline-v1 \
  --correction artifacts/diagnosis-evaluation/experiment-fixture-correction-v1 \
  --reviews evaluation/diagnosis/review-v1.json
.venv/bin/python -m unittest testscript.test_diagnosis_evaluation -v
```

有效基线来自运行时的冻结文件与原始索引；本轮结束后的问题记录会改变工作目录的文档哈希。后续manifest同时记录索引内文件版本与实际工作文件版本，防止source_changed变化时沿用旧生成结果。原run记录的源码已按其哈希归档，重放其原模型输出可离线复核；远端模型重新生成不保证字节一致。没有原索引时可以另建新索引，但必须声明新基线，不能冒用本次run或291片段的口径。

## 本轮发现与修正

两次问题都属于评测样例，未修改产品实现：队列事件初版把observed写成通用对象、缺少Worker归属，后对照生产规则改为数字/Worker身份并加契约测试；实验比较初版把所有均值置null，与两例列表中的1/2ms不一致，后只修正并重测受影响的D13/D15。未修改问题或评分标准以迎合模型输出。合并器校验问题集、索引、模型、产品代码与回放哈希，要求重测范围恰好覆盖变化案例，并拒绝旧报告复核意见套用到新结果。

新增10项评测完整性测试通过，覆盖排名与生产实现一致、分组隔离、金标锚点、无答案分母、离线不调用API、回放边界、事件/实验契约、部分运行报告和修正结果来源校验。既有16项诊断回归在提交检查时重跑通过。首次沙箱内异步单测出现流描述符权限错误后停滞，核验身份后终止该单测进程，沙箱外3.230秒通过；没有把停滞当成功，也没启动数据库或TCP实例。

下一批优先复核语义等价金标与正例，治理当前/历史知识和证据身份表达，再单独处理输出契约稳定性及按问题取证。检索参数、补观测与扩库分别对照评测，本批不依据holdout调参。

本批没有运行生产部署、数据库初始化、新的TCP压测或监控开销评估；未启动常驻测试服务。API客户端与评测进程均已退出，原业务数据和本机nginx.conf保留。原阶段4/5提交已推送，第一批评测改动纳入本次授权提交；提交范围见DECISIONS。
