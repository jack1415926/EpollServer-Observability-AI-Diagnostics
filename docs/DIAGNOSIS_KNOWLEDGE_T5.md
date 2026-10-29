# T5：Worker暂停、退出与快照过期资料主题

当前状态（2026-10-30）：T5首主题已完成，后续T11/T12工程保护已交付且残余优化停止投入；T6案例初版已交付。下文原题失败与索引统计保留当批口径。当前进度见[统一入口](CURRENT_STATUS.md)。

后续：T11/T12已补必要契约和支持检查，原题及新家族对照结果见[最新交付](DIAGNOSIS_EVIDENCE_POLICY.md)。下文T5的3份运行态失败和旧索引/用量保持冻结，不按后续修复重算。

2026-10-27。按用户确认的小批范围完成T5的首个主题：三份一手手册摘要、一个历史案例核对、六题检索与回答评测。没有扩展CPU、数据库或其他主题，也没有补运行指标、修改检索参数或故障策略。T5资料与评测完成不等于运行态回答质量通过。

## 资料、版本与用途

| 入库摘要 | 原始依据 | 补充的知识与使用条件 |
|---|---|---|
| [Linux信号](knowledge/LINUX_SIGNALS.md) | Linux man-pages signal(7) | 区分SIGSTOP暂停、终止和SIGCONT继续；解释为何继续不等于业务健康，不从stale确认信号 |
| [进程状态](knowledge/LINUX_PROC_STATE.md) | proc_pid_stat(5) | 解释T/t/Z与starttime；区分内部读取能力和工具实际暴露字段，不能伪造T状态观测 |
| [等待与回收](knowledge/LINUX_WAIT_STATE.md) | wait(2)，含waitpid | 区分停止/继续/正常终止/信号终止与僵尸回收；不能把父进程接口当成独立监控器可调用的任意进程工具 |

原始依据是本机manpages/manpages-dev 6.17-1中保留版权头的原始roff手册，三页.TH日期均为2026-02-08；原文快照留在忽略的 `artifacts/knowledge-t5/{signal,proc_pid_stat,wait}.man`。在线Linux man-pages 6.19交叉核对相关语义，signal/proc页日期为2026-02-08，wait页为2026-06-05，不声称不同版本全文一致。版本、网址、快照和摘要哈希见 [sources.json](../evaluation/knowledge_t5/sources.json)。本机当前内核为7.0.0-38-generic，仅记录环境信息，未做新的内核行为实验。

Git内是项目助手整理的中文要点，不是官方原文。手册机制使用technical_principle/primary_manual_summary/primary_reference，项目推导单独使用current_contract/project_interpretation/project_document。两者均携带来源网址、原文版本/日期/哈希、获取日期与整理者信息，不把来源性质当作自动可信度评分。白名单增加 `docs/knowledge/*.md`，未知新增或修改仍需策略审阅；没有整站抓取、代码向量化或引入新存储依赖。

这些资料补充了机制与解释边界，不能提供当前进程是否暂停、退出信号或业务健康等缺失事实。[Worker历史案例](DIAGNOSIS_WORKER_STOP_CASE.md)只复核已有报告，区分直接记录的stale/active与历史文档记载的信号注入/恢复；该案例不进入索引，T6整项未完成。

## 评测隔离与结果

输入在 `evaluation/knowledge_t5/{cases,labels,fixtures}.json`，共3道知识题S1～S3、3道运行态题S4～S6。问题、固定接口回放和标注在运行前冻结；标签未发送给模型或入库。运行态数据是合成契约回放，所有监控/日志读取由ReplayTools截获；没有启动或访问真实TCP、MySQL、HTTP或浏览器，也没有发送信号。旧案例与这些合成数据不是同一份证据。

对照使用相同代码和参数。before移除三份新资料并重新建立BM25，全部294个旧片段的ID、正文、来源哈希和向量已逐项核对与T4索引一致；保留T4的用途过滤。after加入三份资料，共303片段；两组都使用BM25/Dense前20、RRF=60、候选20和Rerank前5。未依据旧24题保留集调参，也未对这六题做提示词修正或重试到通过。

| 检查 | 实际结果 | 不能据此推断 |
|---|---|---|
| 本地BM25与真实混合检索 | 两种模式各6题均在扩充后top5取得至少一份指定新资料 | before新资料命中为0是按构造必然，不能把0→6当成Recall提升；命中一个文件也不等于完整证据齐全 |
| 知识回答S1～S3 | 三题人工核对通过，均实际读取资料；正确表达信号/状态/回收与当前观测边界 | 不是外部盲评或开放域准确率 |
| 运行态S4～S6 | 三题均只取workers、search_query为空，无文档证据；人工判定三题均未通过本题全部要求 | 路由intent正确、必要workers存在、JSON/引用合法均不代表语义正确 |
| 输出协议 | 六份报告最终均通过后端校验；S6因额外next_steps_note触发一次有界修正 | 不是六次首次schema全合规；格式修正不修复语义 |
| 工程检查 | 38项针对性Python回归、Node生产报告渲染、Python语法和diff检查通过 | 不是任务服务生命周期、数据库/页面或真实故障复测 |

原始top5对照还显示：S1扩充前主要返回暂停/恢复的历史验收，缺少不可捕获SIGSTOP的直接说明；S2已有身份契约，但缺少T/t/Z的集中解释；S3扩充前含上游学习笔记中过时的“重新fork”说法，扩充后返回wait/信号/状态语义与项目边界。保留原文，不把上游笔记当当前自动补起策略。S5虽命中新wait资料，其top5并不保证包含dead完整判定规则，进一步说明文件命中不是证据覆盖指标。

六份报告的44条fact和12条假设均逐条核对，并检查summary、unknowns与next_steps；事实字段读数基本正确仍不能抵消summary和假设的机制错误。审阅者为本项目助手，不是独立专家；具体支持标签、失败位置及报告哈希见 [review.json](../evaluation/knowledge_t5/review.json)。假设得到支持仅指可作为明确未验证的候选，不表示根因已证实。

## 未解决的问题与同类范围

| 样例 | 现象与依据 | 后续归属 |
|---|---|---|
| S4 | 没有文档证据，却在summary/部分原因中加入共享内存或心跳机制，与文件快照设计不符 | T11最小必要契约取证；T12机制与引用支持检查 |
| S5 | 将SIGSTOP和一般指标不可读列为dead原因；读取器实际区分T与Z/X/x，以及dead与process_unavailable | T11状态语义取证；T12跨状态矛盾检查 |
| S6 | 承认不可读不证明退出，但遗漏“不自动补起”，部分假设把身份不符混为读取失败 | T11项目策略取证；T12状态和策略支持检查 |

确定的取证路径原因是分类结果未选文档，后台按本题白名单也不暴露文档工具；不是资料没建库，也不能据此单独归因于Embedding或Reranker。此前有限路由验收未覆盖这组字段语义题，v1已有未解决的类似语义问题，不称为修复后复发。核对范围为路由、allowed工具构造、Tools/ReplayTools、读取器CheckProcess和报告各章节。生产取证策略、提示词和采集器本轮均未因此修改；统一问题记录见[开发问题记录](DEVELOPMENT_ISSUES.md#2026-10-27t5资料命中与运行态取证遗漏)。

## 索引、证据与交付状态

测量索引 `artifacts/knowledge-t5/index.npz` 固定为303片段，SHA-256 `18a40f1f85db57c90a4b82d90878342afd94aad9ceab61f5858dab313a2cfeb5`。当时的25份来源正文和策略另存 `measured-sources/` 与 `measured-corpus-policy.json`；原始返回、排名、用量及报告保留于 `local/`、`remote/`。收尾追加的问题记录和阶段说明不回写该测量；这些评测进度不参与默认检索。六题已评阅，仅作开发回归，不能再冒充未见保留集。

最终当前索引另在 `final-index.npz` 构建并原子安装到 `artifacts/diagnosis/index.npz`：25份来源、305片段，SHA-256 `91af1bd8050d2f330894b1152caa33e639df551ded3e7d53103c642156cd4fb8`。原生产294片段索引保存为 `pre-t5-index.npz`。最终来源/策略与documents()一致、生产Retriever加载通过；最终收尾索引没有重新执行质量评测，不能把303片段上的结果改称305片段的新测量。T4、v1冻结索引与本机nginx.conf均核验未变。

初次建库复用294个向量、新建9个；收尾复用300个、新建5个，随后仅离线刷新策略元数据。共3批Embedding建库调用。真实评测43次API请求：Embedding 9、Rerank 15、DeepSeek 19；Rerank usage未返回，保留缺失。共享同题查询/重排缓存，耗时不是各组独立冷启动代价；未查询账单，不估算费用。脱敏汇总、运行manifest和证据哈希见 [acceptance.json](../evaluation/knowledge_t5/acceptance.json)。

没有新增常驻实例、临时凭据或服务数据目录；短命构建/评测进程均已退出，原业务和本机配置未改。未重启服务、部署、提交或推送，未做新性能负载、正式库初始化、数据库/浏览器或ServiceTests生命周期复测。已有Python服务若需使用新索引，须另行重启。

复现入口（新输出目录，不覆盖已有结果）：

```bash
.venv/bin/python -m unittest testscript.test_corpus_layers -v
.venv/bin/python -m diagnosis.retrieval --offline
.venv/bin/python -m diagnosis.evaluate_knowledge --output artifacts/knowledge-t5-local-new
# 真实付费模型；监控数据仍为合成回放
.venv/bin/python -m diagnosis.evaluate_knowledge --remote --output artifacts/knowledge-t5-remote-new
```

历史测量的索引与策略哈希绑定；当前代码默认拒绝策略不匹配的旧索引，不能把新正文/策略偷偷混入旧结果。重新评价当前实现使用当前新索引及新输出目录；旧测量只按其归档材料核对。
