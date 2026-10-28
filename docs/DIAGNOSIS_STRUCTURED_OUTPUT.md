# 诊断报告的Strict输出与后端校验

后续更新：用户继续追问400后，已确认Responses API支持原生json_schema，当前最终报告已迁移。见[Responses修复记录](DIAGNOSIS_RESPONSES_JSON_SCHEMA.md)。下文beta最终函数方案为先前批次的历史实现和验证，不代表当前最终报告协议。

2026-10-26，按用户要求处理v1的格式失败。保留原基线，不把重新生成的成功报告覆盖旧失败；召回率审计另见 [Recall逐阶段审计](DIAGNOSIS_RETRIEVAL_AUDIT.md)。

## 能力核验与实际边界

原实现已经使用 `response_format={"type":"json_object"}`，并在提示词中提供Pydantic schema，最后进行本地校验。它只约束JSON语法，不能阻止多余字段。v1的W3/live_only、E2/live_only、D1/hybrid_rerank分别多出顶层evidence_ids、conclusion_note、facts_extra_unused，被extra=forbid拒绝。

[DeepSeek Chat Completion文档](https://api-docs.deepseek.com/api/create-chat-completion/)只列出text/json_object；本机配置和deepseek-flash的最小 `response_format=json_schema` 请求实际返回400。[Tool Calls官方文档](https://api-docs.deepseek.com/guides/tool_calls/)提供beta strict Function Calling；需要beta路径、每个function的strict=true、每个object所有字段required及additionalProperties=false。字符串minLength/maxLength、数组minItems/maxItems不受支持。

最小beta strict报告请求被接受并通过Pydantic校验。**但接受strict字段不等于本次接口能提供可靠的约束生成保证**：在一个额外字段对抗探针中，schema只允许ok、禁止额外字段，实际参数仍为 `{"ok":true,"extra_probe":null}`；另一份报告的首次参数还触发json_invalid。后续独立探针又返回符合schema的结果，说明不能用一次成功推断始终严格遵循。这个限制与官方strict声明不一致，本轮无法确定服务端原因，不归因于某个未核验的模型实现。

原始最小能力核验在 `artifacts/diagnosis-structured-capability.json`；额外字段反例与三次最终报告探针在 `artifacts/diagnosis-structured/final-probes.json`。脱敏摘要在 `evaluation/diagnosis_v1/structured_output_check.json`。没有将HTTP 200或tool_calls结束直接统计为schema正确。

## 当前实现

- Agent七种只读工具都带strict=true；请求自动使用DeepSeek beta端点，已有/v1或/beta配置不会拼出重复路径。参数仍经过本地白名单、字段、UUID、长度与预算校验。
- 最终阶段只提供 `submit_diagnosis_report`，通过named tool_choice强制选择它。它是返回报告的容器，不执行操作，也不交给取证工具派发器。工具名、单个调用、调用ID和参数字符串均检查；普通文本不会作为报告兜底。
- 服务端schema由现有Analysis模型派生，展开局部引用、移除该接口不支持的长度/条数关键字；保留对象required/additionalProperties=false、字段类型与结论enum。facts/possible_causes的evidence_ids项限定为本次实际E编号enum。
- Pydantic完整保留原有额外字段拒绝、长度、条数和非空约束。它是成功报告交付的最终格式边界；引用存在性仍单独检查。schema正确不证明引用支持自然语言判断。
- 校验失败时只允许**一次**最终报告修正，反馈字段位置与错误类型，不包含错误输入正文。不会重新读取工具、重复提交任务或隐式重试HTTP。第二次仍不合格则失败返回，绝不悄悄删除多余字段后当作成功。
- provider遇到finish_reason=length明确报deepseek_output_truncated，不能把截断参数当完整报告。400、认证/网络失败或非工具返回不会降级为宽松文本，也不触发网络重试。
- 报告增加output_validation：requested_protocol、validated_by=pydantic、repair_errors，明确记录请求的服务端模式与后端修正原因。外层报告schema_version仍为1、数据库和网页原有字段不变。

继续使用4000输出tokens、已有任务总期限和30秒单次HTTP上限；修正也计入总期限。没有增加依赖、修改模型、业务库、TCP协议或自动修复能力。服务端未支持的约束由后端兜底，不对外宣称所有生成都会成功。

## 验证

新增回归先在旧实现失败：真实Agent→Providers→MockTransport路径的最终请求没有tool_choice；修复后通过。测试还覆盖beta URL归一化、所有工具strict、实际E编号enum、额外字段不静默删除、长度/条数有界修正、失败不无限重试、400/截断/普通文本拒绝，以及探针不能把“接受strict”误认成“强制遵循”。

```bash
.venv/bin/python -m unittest testscript.test_diagnosis.AgentTests \
  testscript.test_diagnosis.ProviderTests testscript.test_diagnosis.RetrievalTests \
  testscript.test_diagnosis_evaluation testscript.test_diagnosis_structured -v
# 只验证额外字段契约；一次付费调用。失败应调查，成功也不是普遍保证。
.venv/bin/python -m diagnosis.probe --strict-contract
```

22项针对性测试通过，日志在 `artifacts/diagnosis-structured/unit-tests.log`。原有工具/Agent测试替身改为返回最终函数参数，测试真实协议变化，未绕过后端引用校验。未重跑已记录不稳定的服务过载/退出用例，状态见首次基线。

真实API回放复测三个旧格式失败问题，每题完整RAG和live_only，共6次：

| 问题 | 完整RAG | 仅运行证据 |
|---|---|---|
| W3：过期Worker与重启 | 成功 | 成功，触发一次后端修正 |
| E2：缺失均值与分位数 | 成功，触发一次后端修正 | 成功 |
| D1：MySQL持锁事务未知 | 成功 | 成功，触发一次后端修正 |

这批6/6是“校验及最多一次修正后的成功”，不是6次首次schema全通过。早批没有保存首次修正原因；随后新增修正元数据，用已有证据另做3次最终报告探针：W3和D1无修正通过，E2因json_invalid修正一次后通过。该3次是仅最终生成，不伪称完整诊断重跑。所有样本独立保留，没有将它们合并成新的诊断准确率。

六次完整回放分别在 `artifacts/diagnosis-structured-w3/`、`-e2/`、`-d1/`，含各自脚本/提示词/索引哈希与用量。没有重新压测、启动数据库或浏览器；原系统服务、本机配置不变。格式修复不代表语义错误已修复，Recall也没有因本次改动获得提升。未部署、提交或推送。

## 文档与索引维护

v1原索引已保存为 `artifacts/diagnosis-eval-v1/index.npz`，与baseline_manifest中的哈希一致；旧报告、标签、指标和审阅不改写。当前问题记录与阶段运行契约在测量结束后追加，再复用旧正文向量更新当前索引，使后续诊断能识别新的输出协议。新旧索引不混作同一次测量；原召回审计显式使用冻结索引。
