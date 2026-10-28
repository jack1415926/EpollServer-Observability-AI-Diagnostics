# 诊断修改后的验证核对

后续T11/T12工程与本批验收已完成，最新局部校验、源审阅、真实HTTP/MySQL与资源状态见[交付记录](DIAGNOSIS_EVIDENCE_POLICY.md)。本页下文保留前批当时结果，不作为当前完整故障或全站浏览器复测证明。

后续T4语料分层已完成建库、用途过滤和检索对照，当前索引已重建，见[T4实际结果](DIAGNOSIS_CORPUS_LAYERS.md#实际结果2026-10-27北京时间)。以下“索引待重建”和T4待办是本次审计当时的历史状态；T5～T12及新版完整故障/独立质量验收仍未整项完成。

2026-10-26，针对“修改后是否实际测试”的核对。下述运行发生在上次暂停前；恢复后只核验已有证据和哈希、补齐记录，没有新增模型或Embedding调用。功能通过与诊断质量达标分开判断。

## 实际验证

| 修改或边界 | 已完成的验证 | 限制 |
|---|---|---|
| 路由、结构输出、Agent、评测和任务服务 | 37项合并Python回归，3.415秒，OK，包括原6项ServiceTests | 沙箱外运行；测试替身验证工程行为，不是37次真实故障诊断 |
| Responses兼容与分类 | 前批4次真实报告首次校验通过；13题真实分类、6条固定工具回放通过 | Chat的json_schema 400已适配；仍有服务端schema违约反例，保留后端校验；开发题集不是通用准确率 |
| 五类真实任务 | Drogon→Python→真实模型/检索→私有MySQL；逐条精确SQL读回；重启后五条报告保持一致 | knowledge只取文档，runtime只取workers，experiments只取实验列表，范围外/澄清无证据；不是仅重放已有报告 |
| 实际浏览器 | 查看五类报告，提交并完成一条新的null与0知识问题 | 知识回答不显示固定根因提示；范围外/澄清不显示空章节 |
| 宽度与自动可访问性 | 桌面1280px及窄屏390px无横向溢出；窄屏axe 4.12.1：27项通过、0违规、0待检查 | 仅本次诊断页面状态，不代表全站WCAG认证；截图和窄屏JSON已保存 |
| 生命周期 | 私有MySQL预检通过；管理员SHUTDOWN、等待退出后删除数据；测试Python/HTTP与浏览器已退出 | 原有MySQL身份核验通过，业务数据及本机nginx.conf未改 |

真实链路五条任务耗时29.9秒，不用于承诺延迟或成本降低。该次为`--smoke --routing`，未启动TCP实例，Worker数据为不可用场景；没有复测真实Worker暂停/恢复或完整故障隔离。脚本通用输出“fault isolation”不能扩大本次验收范围。

浏览器新增任务ID为`e4af84cb-01c6-426d-994d-dcad7bce7c51`。用户暂停中断后续检查，未取得它的独立API/SQL读回证据，也未在本轮重验折叠状态跨轮询保持、断开按钮清空。旧版通过结果不算新版复测。

## 服务单测退出挂起

沙箱内有界执行超时返回124。最小探针记录asyncio唤醒写入`PermissionError errno=1`，主线程停在selector与Runner.close。未修改的原6项ServiceTests在沙箱外3.196秒通过，随后包含它们的37项合并回归也正常退出。当前复现支持沙箱限制阻断跨线程唤醒，不据此认定生产服务存在已确认的取消竞态；没有给生产代码添加绕过逻辑。

此前漏测原因是未区分断言通过和运行器退出，也未保存唤醒错误。同类范围核对lifespan取消/gather、worker执行/保存和异步测试清理。一次宿主机通过不能排除所有生产退出风险；本次消除的是已复现环境下的阻塞疑点。

## 证据及复现

- `artifacts/verification-audit/combined-host.log`：37项回归；`service-tests-host.log`：原6项服务测试；`wakeup-sandbox.log`：EPERM与退出栈。
- `artifacts/stage3-live-q39lgjmv/`：私有数据库生命周期预检。
- `artifacts/stage5-live-6gv7zb5l/`：五类真实报告、manifest、服务退出日志、浏览器截图、narrow-layout.json及a11y-narrow.json。
- 联调manifest的10个源文件/二进制/索引哈希在恢复时全部匹配当前文件；文档修改不回写历史manifest。原始证据留在忽略目录。

无付费调用的回归入口（服务测试需要允许asyncio本地唤醒的执行环境）：

```bash
timeout --signal=TERM --kill-after=2s 30s .venv/bin/python -m unittest \
  testscript.test_diagnosis testscript.test_diagnosis_evaluation \
  testscript.test_diagnosis_structured testscript.test_diagnosis_responses \
  testscript.test_diagnosis_routing -v
node testscript/test_diagnosis_render.js
```

需要远端配额的真实链路入口，配额恢复后按需运行；`--hold`仅用于交互式PTY浏览器验收：

```bash
.venv/bin/python testscript/test_stage3_live.py --database-only
.venv/bin/python testscript/test_stage5_live.py --smoke --routing --hold
```

## 剩余工作

1. 独立新问题集评估路由误分、必要证据遗漏及答案支持关系，对比收益、退化、用量和延迟；现有13题不能代替。
2. T4～T6分层当前契约/历史/进度，补一个主题的一手资料与可验证案例，再做检索对照。原47.92%仍为冻结锚点评分，没有新的Recall提升结论。
3. T7运行证据、T8实验样例解耦、T9/T10切分与参数、T12语义引用检查仍待实施；T11仅完成路由/最小取证增量。
4. 新版真实TCP故障场景、中断的UI交互检查、正式监控开销和部署分别验收，本次smoke不能覆盖。

本次完成验证审计收尾，不将上述待办标为完成。更新已被索引收录的阶段/问题文档后，现有知识索引将过期；为避免新增付费Embedding，本轮不重建。恢复模型服务前须先重建索引，旧评测继续用各自冻结索引。未提交、推送或部署。
