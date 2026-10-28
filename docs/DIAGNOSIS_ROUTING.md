# 按问题路由与最小取证

后续更新：T11/T12已加入必要契约闭包、明确读取纠偏、原文实验角色绑定、普通观测回答、支持定位及复现/拒绝记录；见[当前交付](DIAGNOSIS_EVIDENCE_POLICY.md)。以下“遗漏后不能补文档”等内容描述本次路由初版；当前只补固定资料，不自动增加未选运行端点。

2026-10-26，按用户要求修复“所有问题先读健康、Worker、事件、实验，再检索文档”。原KBrag的service/tech分类用于客服与产品手册，当前实现没有导入其业务分类、产品路由、qid或会话逻辑。本批独立实现诊断领域路由，不修改检索排名参数。

## 当前流程

```text
完整问题 → Responses JSON Schema分类 → Pydantic与后端取证规则
  技术知识 → 文档查询 → 知识回答
  当前故障 → 选中的指标/事件/日志，按需文档 → 受限工具循环 → 分析报告
  实验分析 → 指定两条实验对比，或最近实验列表，按需文档 → 分析报告
  范围外   → 范围提示，不取证
  信息不足 → 请求补充信息，不取证
```

路由模型只看到用户问题，不预读监控、日志、实验或知识库。由QuestionRoute约束intent、runtime_sources、search_query、clarification、experiment_action及比较UUID。完整问题进入分类，检索query由路由保留关键条件且最多500字符，替代简单截取前500字符。

| 路由 | 允许的初始读取 | 后续执行与回答 |
|---|---|---|
| knowledge | 必须有文档query；不能选择运行来源 | 直接检索后回答，不运行取证Agent；KnowledgeAnswer不要求结论、可能原因或非空unknowns/next_steps |
| runtime | 至少一个必要来源：health/workers/events/logs；可选文档query | 只向Agent暴露本题已选来源及已选文档工具，实际派发再次检查；最多原两轮/每轮四个请求 |
| experiments | list读最近实验，compare读明确的两条UUID；可选文档 | 不读当前指标或日志，不跑额外取证循环；使用原分析契约，历史记录不表示当前状态 |
| out_of_scope | 无 | 固定范围提示，不复制电商客服业务，也不再额外生成故障报告 |
| clarify | 无 | 展示简明补充请求；错误分类schema/规则冲突也转此路径 |

路由失败不能退回全量采集。模型输出违反路由schema或规则时返回clarify、status=invalid_route；API认证/网络等失败则保持任务失败语义，不编造分类或调用全部工具。

取证采用工具列表过滤和后台派发双重限制，模型自称另一路由或请求隐藏工具都不能扩大访问。一次任务中，同工具同参数复用已有结果和E编号，避免“初始已读日志，模型又读一遍”；不延长证据时间戳，也不在任务之间共享缓存。报告陈述已取得证据时点，不能把缓存当成后续实时变化的证明。

## 实验身份和澄清

比较时baseline/candidate按用户语义由路由提取，后台核对它们与问题中的两条UUID完全一致；不能编造ID或擅自取最近两条。没有两条明确ID、路由ID不匹配时先澄清。未指明角色时按问题顺序第一条作基准、第二条作候选；用户明确“候选在前、基准在后”时保留角色，不按出现顺序颠倒。中文紧邻UUID也可识别，避免正则word boundary把中文当字母导致漏匹配。

本批仍只支持已有的最近100条列表与两UUID比较，不添加历史实验详情、时间过滤或相似查询。跨当前故障与历史对比、缺少明确目标等请求，分类需保守澄清或明确选择一个受支持任务，不暗中查询全部资料。

## 输出与兼容性

外层报告保留schema_version=1，增加kind与routing。运行/实验分析沿用Analysis；技术回答经KnowledgeAnswer校验后组装兼容字段（conclusion=knowledge_answer、possible_causes=[]），空的未知项/步骤不强制填写。范围外与澄清结果由后端组装，不制造事实或证据。所有实际事实和假设仍核验引用存在；无证据时生成schema把facts/possible_causes上限设为0，避免空enum无效，也不允许假引用。

网页按kind展示技术资料回答、历史实验分析、需要补充信息或支持范围提示，空章节不显示；旧报告没有kind时继续使用原故障结论提示。知识题不会再出现“证据不足，无法确定根因”的固定横幅。知识、实验和无取证任务不再标成当前监控实例来源。数据库仍保存JSON，无表结构迁移；任务接纳/存储、鉴权、并发数、总期限保持不变。“不读运行来源”不表示绕过任务表持久化。

## 验证与证据

后续已补齐新版真实HTTP/模型/MySQL五类任务、重启查询和浏览器验证，并定位沙箱退出阻塞，见[验证核对](DIAGNOSIS_VERIFICATION_AUDIT.md)。下述记录仍对应最初路由批次，不代表后续未进行联调。

最小回归先在旧代码失败：真实diagnose→Tools→HTTP替身路径中，问“epoll原理”触发了health读取。修复后同一测试通过，且文档retriever只收到一次有针对性的query。

```bash
.venv/bin/python -m unittest testscript.test_diagnosis.AgentTests \
  testscript.test_diagnosis.ProviderTests testscript.test_diagnosis.RetrievalTests \
  testscript.test_diagnosis_evaluation testscript.test_diagnosis_structured \
  testscript.test_diagnosis_responses testscript.test_diagnosis_routing -v
node testscript/test_diagnosis_render.js
node --check monitor/web/app.js
make -C monitor http
# 真实分类模型；完整路径使用固定工具回放，不访问运行中的服务或业务库
.venv/bin/python -m diagnosis.evaluate_routing --cases evaluation/routing_v2/cases.json \
  --output artifacts/diagnosis-routing-new-run
```

31项Python针对性测试通过，包含六项路由测试（其中子场景覆盖范围外、澄清、非法分类、缺少实验ID和分类API失败），并保留真实Agent预算/引用回归。Node运行生产renderDiagnosis代码核验新/旧报告、空章节和文本渲染；这不是浏览器/可访问性认证。HTTP重新嵌入页面资源并通过原Make构建。

首轮12题分类符合预期、5条完整回放通过；补充实验角色绑定与任务内重复读取复用后，当前版本13题全部符合预期、6条完整回放通过。该题集为本项目助手编写的开发验收，没有独立保留集，不声称通用分类准确率100%。完整工具数据均为合成契约回放，不称为真实CPU/DB或故障注入。

当前回放的实际读取：K1仅search_technical_docs；R2仅read_server_logs（重复请求复用）；E1/E3仅compare_experiments且基准/候选一致；O1/C1没有工具读取、只发生分类调用。分类本身增加一次模型请求，本轮不据此承诺总体成本或延迟降低。路由可能误分、遗漏所需来源；后端只提供本题选中的工具，遗漏需要说明缺证据或新问题重新分类，不自动扩权。语义支持正确性仍需独立评测。

首轮证据在artifacts/diagnosis-routing-v1；当前结果、调用用量、代码/题集/索引哈希在artifacts/diagnosis-routing-v2。版本化题集、manifest和脱敏取证核验在evaluation/routing_v2；原始报告不纳入Git。测量时索引已归档该证据目录，文档更新后再更新当前知识索引，不把新索引算成这次测量。

同类排查覆盖diagnose唯一入口、service实际调用方、Tools派发、ReplayTools替身、所有Provider测试替身、报告schema、评测工具及网页渲染。未修改原KBrag、TCP服务、业务数据或本机nginx.conf，没有新增常驻测试实例，无目录清理需求。未重跑原24题诊断质量基线、性能负载、数据库持久化/浏览器联调或已知不稳定的服务生命周期用例；未部署、提交或推送。
