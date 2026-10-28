# DeepSeek json_schema 400修复

2026-10-26。用户追问json_schema请求400能否修复后，补查官方Responses API并完成适配。此前只核验Chat Completions，由其400概括成“DeepSeek不支持json_schema”不准确；此处更正，并保留原始试验记录。

## 根因与最小对照

相同模型、密钥配置及最小schema：

| 请求 | 实际结果 |
|---|---|
| `/chat/completions` + `response_format.type=json_schema` | 400，`This response_format type is unavailable now` |
| `/responses` + `text.format.type=json_schema` | 200、status=completed、输出符合最小schema |
| `/responses` + 完整Analysis schema | 200，报告通过Pydantic；包含$defs/$ref、长度、条数约束，无需为beta工具删掉这些关键字 |

[Chat Completions官方文档](https://api-docs.deepseek.com/api/create-chat-completion/)与[Responses官方文档](https://api-docs.deepseek.com/api/create-response/)是两个不同的请求协议。后者明确列出text.format的json_schema类型。400的根因是把该格式放到了当前不接受它的接口，不是本地Pydantic字段拼写、认证失败或模型完全不支持此请求格式。

当前正确的最终生成请求形态为：

```json
{
  "model": "deepseek-flash",
  "input": [{"role": "user", "content": "输出JSON：ok为true"}],
  "reasoning": {"effort": "none"},
  "max_output_tokens": 4000,
  "text": {
    "format": {
      "type": "json_schema",
      "name": "probe",
      "schema": {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": false
      }
    }
  }
}
```

示例为最小可调用schema；生产报告使用Analysis.model_json_schema()并限制本次E编号。无需新密钥、SDK或模型切换。

## 改动范围与失败语义

`Providers.structured()`专用于最终报告，POST到原生Responses接口。系统/用户/助手消息保留角色，历史Chat工具调用及工具结果转换成配对的function_call/function_call_output，拒绝重复、孤立和未配对调用ID。最终请求不提供可执行工具。URL配置是根地址、/v1或/beta时均归一到正确Responses端点。

Agent七种取证工具仍沿用原Chat调用，不迁移整个工具循环。最终报告直接读取completed响应中的output_text；函数返回、空文本、错误协议、refusal、失败/未完成状态均拒绝。max_output_tokens截断明确报错，不当完整JSON处理。取证已结束时不会因报告生成而重复执行工具。

本地Pydantic、真实引用编号核验及最多一次最终报告修正保留。修正历史改为助手文本+字段错误反馈，仍使用同一完整schema。`output_validation.requested_protocol`更新为deepseek_responses_json_schema，validated_by仍为pydantic。评测记录同时保存Responses的status及incomplete_details，不用空的Chat finish_reason冒充完成。

## 接口接受与约束保证分开验证

400修复不表示服务端已被证明有可靠的约束解码。单独的极小对抗请求要求追加extra_probe=null，schema明确additionalProperties=false；省略strict与显式strict=true两次均返回 `{"ok":true,"extra_probe":null}`。因此当前服务对该请求格式的支持已确认，但不能按“始终强制遵循schema”来依赖。服务端为何未遵循的根因未查明，本次不猜测；也没有以忽略多余字段掩盖问题。

```bash
# 只做一次Responses额外字段契约探针，不调用Embedding/Rerank
.venv/bin/python -m diagnosis.probe --json-schema-contract
```

本次该契约探针按预期检测到越界并返回1。普通最小请求及完整schema请求成功是不同检查，不能只用200宣称强保证。原beta工具探针 `--strict-contract` 保留用于对照；默认依赖预检已改为Responses json_schema，以覆盖实际最终生成路径。

## 验证与证据

新增回归在旧代码首先因缺少原生structured适配失败；修复后覆盖原生端点/完整schema、Chat→Responses工具历史配对、分段文本、未完成/失败/refusal、越界字段拒绝与有界修正。保留原工具预算和引用真实性回归。

```bash
.venv/bin/python -m unittest testscript.test_diagnosis.AgentTests \
  testscript.test_diagnosis.ProviderTests testscript.test_diagnosis.RetrievalTests \
  testscript.test_diagnosis_evaluation testscript.test_diagnosis_structured \
  testscript.test_diagnosis_responses -v
```

25项针对性测试通过。D1和R2两题各完整RAG/live_only共4次真实诊断回放，全部首次通过Pydantic，repair_errors均为空，Responses均completed；这不是新诊断准确率。四次模型均未追加工具调用，因此另以一条合成但实际发送的配对工具历史核验转换路径，接口200且输出正确；不把合成历史说成真实工具执行。

证据位于 `artifacts/diagnosis-responses-schema/`：capability.json含脱敏400错误与200对照、enforcement.json含两种strict设置的实际返回、history.json含合成历史结果；真实回放分别在 `artifacts/diagnosis-responses-d1/` 与 `-r2/`。脱敏汇总与报告/manifest哈希在 `evaluation/diagnosis_v1/responses_schema_check.json`，完整成功报告不纳入Git。

v1原始索引/标签/结果不改写；上一批当前索引另存 `artifacts/diagnosis-structured/index.npz`。文档追加完成后更新当前知识索引，复用未变化正文向量；旧回放仍以自身manifest为准。未改检索参数、TCP、业务库、凭据或UI；未启动常驻服务或新性能负载，无新增服务清理工作。已有服务生命周期不稳定问题未在本次重跑或修复，未部署、提交或推送。
