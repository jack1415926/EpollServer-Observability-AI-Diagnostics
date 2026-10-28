# 阶段 5：RAG 与只读 Agent 诊断

2026-10-25。用户确认完整链路：网页 → Drogon 异步入口 → 本机独立 Python 服务 → 远端模型与检索 API → 独立 MySQL 任务/报告表。允许发送相关脱敏文档、指标和日志。工具只读，不执行 Shell、业务写入或自动恢复。

## 依赖与来源

- DeepSeek V4.1 Flash：`https://api.deepseek.com`，已核验官方调用 ID `deepseek-flash`，模型列表与实际 JSON 生成均通过。
- Embedding：硅基流动 `https://api.siliconflow.cn/v1/embeddings`，`Qwen/Qwen3-Embedding-8B`，实测 4096 维。
- Rerank：同一服务 `/rerank`，`Qwen/Qwen3-Reranker-8B`。
- KBrag-V6 本地参考 HEAD 为 `472a69940e61fd38c70d12c727419286e24742a8`，与来源记录一致。参考其标题分段、BM25/Dense、RRF、20 条候选远端重排及受限 Agent 方法；产品手册、路由、多模态系统和产品问答提示词保持独立。
- 小型知识库使用 NumPy 精确向量内积，执行与归一化 IndexFlatIP 相同的计算，不增加 FAISS 编译依赖。未继承 KBrag 的效果或性能数字。

凭据和本机服务令牌保存在忽略的根目录 `.env.stage5`，权限 0600；Python 启动时读取，已有环境变量优先。Drogon 需由 shell 导出配置。`diagnosis/.env.example` 只有非敏感默认值和空凭据字段。

## 构建与启动

沿用 Make；当前开发环境 Python 3.14，未验证其他版本。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-diagnosis.txt
make -C monitor http
.venv/bin/python -m diagnosis.probe
.venv/bin/python -m diagnosis.retrieval
```

管理员在独立监控库应用更新后的 `sql/monitor.sql`，只增加诊断表权限，不更改业务库：

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON epoll_monitor.diagnosis_task TO 'epoll_monitor'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE ON epoll_monitor.diagnosis_report TO 'epoll_monitor'@'127.0.0.1';
```

配置 `EPOLL_MONITOR_DB_*`、`EPOLL_MONITOR_PORT`、`EPOLL_MONITOR_TOKEN`、`EPOLL_DIAG_TOKEN` 后，分别在两个终端启动：

```bash
.venv/bin/python -m diagnosis.service
```

```bash
set -a
. ./.env.stage5
set +a
./monitor/build/epoll-monitor /absolute/path/to/master-manifest.json 8080
```

两端仅绑定 127.0.0.1。Python 默认 8090，`EPOLL_DIAG_PORT` 须在两端一致；默认监控端口 8080，Python 工具的 `EPOLL_MONITOR_PORT` 须匹配实际端口。浏览器只用监控令牌，内部诊断令牌和模型密钥不下发页面。

只运行一个 Python 服务进程/副本，不启多个 uvicorn workers；未实现多副本任务协调。默认不启动或部署这些服务。

## 知识与引用

离线语料包含 README、实施决策、问题记录、阶段说明、`docs/upstream/*.md` 和受审阅的 `docs/knowledge/*.md` 资料摘要。按标题及重叠行窗口切分，保留相对文件、行号范围、标题、整份文件 SHA-256 和来源范围。向量与元数据通过单个 `.npz` 原子替换发布，不加载 pickle；索引位于忽略的 `artifacts/diagnosis/index.npz`。修改文档、模型或分段逻辑后重建，替换索引后重启 Python。

检索为 BM25/Dense 各前 20 → RRF(k=60) → 合并前 20 → 远端 Rerank 前 5。查询时复核文件哈希，`source_changed` 标记旧版本；早期阶段说明不代表当前完成范围，上游文档显式标为历史参考。实时快照和诊断报告不自动进入索引。

工具证据包含实际 E 编号、来源/范围、取得时间、脱敏摘录、完整脱敏内容哈希及截断标记；最多 18 条，每条正文最多 20000 字符。片段文件哈希与工具证据哈希用途不同。报告中的事实和可能原因必须引用存在的编号，否则失败；存在性校验不保证自然语言解释必然正确。

七种工具：健康与采集状态、Worker 指标、近期事件、受限日志尾部、历史实验列表、两次实验比较、技术文档检索。监控工具只访问配置的本机端口，不接收任意 URL、SQL、Shell 或文件路径。日志路径由管理员通过 `EPOLL_DIAG_LOG_PATH` 固定（默认 `server/error.log`），拒绝最终符号链接和非普通文件，仅取末尾 64 KiB/100 行；不宣称已核验日志属于当前实例。

证据发送前与报告落库前均脱敏。文档/日志中的指令视为数据。每轮最多执行四个工具，共两轮；多余调用得到限制错误，不执行。权限约束不等于证明对所有提示注入免疫。

## 任务与故障语义

独立 `diagnosis_task` 保存问题、状态、错误码和时间；`diagnosis_report` 保存相同 UUID 的 JSON。完成状态与报告在同一事务写入；重复完成按 UUID 更新。数据库不可用、缺表或缺权限时拒绝新任务。

状态为 queued → running → succeeded/failed/timed_out；重启后未完成任务标为 interrupted，不自动重调模型。报告分事实、可能原因、未知、建议和证据；insufficient_evidence 明确无法确定根因。建议未被自动执行。

并行两个任务，最多八个排队，总数最多十个。排队超过 30 秒不再调用模型；执行总期限默认 90 秒，可用 `EPOLL_DIAG_TIMEOUT_SECONDS` 配置 1～300 秒。远端请求最多 30 秒并受任务异步取消期限约束，无隐式重试。SQL 连接/读/写各 2 秒，不承诺 OS 故障下的硬实时退出。

完成后 SQL 保存失败时保留槽位与结果重试，不重复模型调用；内部健康状态显示待保存数。进程崩溃仍可能丢失该内存结果，重启按未完成任务标记中断。停机取消执行并尝试记录中断，排队任务由下次启动处理。不是持久化分布式任务队列。

查询期限七天，过期立即不返回；新任务提交时每表最多删 100 条过期数据，无独立定时清理。因此没有新提交时物理记录可能保留更久，长期归档由管理员管理。结构化内容先递归脱敏值再序列化，避免密码赋值文本吞掉 JSON 引号。

## 接口与网页

| Drogon 接口 | 方法 | 行为 |
|---|---|---|
| `/api/v1/diagnoses` | POST | `{ "question": "..." }`，202 返回 UUID |
| `/api/v1/diagnoses` | GET | 最近 100 条未过期任务 |
| `/api/v1/diagnoses/{uuid}` | GET | 状态、错误码和完成报告 |

复用现有 schema_version/timestamp/code/validity/data_valid/data 包装；stored 表示控制面数据可用，不表示 TCP 健康。问题 1～1000 字符，拒绝全空白、额外字段、查询参数、非法 UUID、错误方法和超过 4096 字节的正文。Drogon 最多八个异步转发在途，等待最多四秒，不在网络事件循环中执行模型。

未配置内部令牌返回 diagnosis_disabled；Python 不可达返回 diagnosis_service_unavailable。Python 内部只有鉴权后的 `/internal/health` 与任务接口。接纳成功但回复中断时，列表可找到用户未收到 UUID 的任务；没有客户端幂等键。

网页新增 AI 诊断页，支持提交、列表、状态轮询、报告和展开来源，按纯文本渲染模型输出。断开清空记录与报告，旧请求通过 generation 隔离；令牌不持久化。每两秒刷新且不重叠请求，已完成且未变化的报告保持 DOM，便于读证据和键盘操作。

## 分层验收

2026-10-26当前修改的补测见[验证核对](DIAGNOSIS_VERIFICATION_AUDIT.md)：37项合并回归、五类真实任务及重启读回、浏览器提交与窄屏检查通过；本次为smoke，未复测真实TCP故障。文档更新后索引待重建。

```bash
.venv/bin/python -m unittest testscript.test_diagnosis -v
PYTHONPATH=testscript python3 -m unittest test_http_monitor test_event_rules test_import_experiment test_stage3_lifecycle -v
.venv/bin/python testscript/test_stage3_live.py --database-only
.venv/bin/python testscript/test_stage5_live.py --smoke
.venv/bin/python testscript/test_stage5_live.py
# 在交互终端保留同一完整联调的服务供浏览器；回车后清理
.venv/bin/python testscript/test_stage5_live.py --hold
# 只复用已有诊断报告，独立重跑浏览器，无新模型调用或 TCP 故障
.venv/bin/python testscript/test_stage5_live.py --smoke --hold --report /path/to/diagnosis.json
```

私有 MySQL 先完成一条真实任务/报告写入读回和幂等完成。完整模式使用自己的真实 TCP 实例，核验身份后暂停/继续 Worker，复用两份既有实验，不生成性能负载。数据库管理员 SHUTDOWN、wait 确认退出后才删目录；TCP 私有组退出确认后才删 0600 配置。失败保留诊断证据。

## 本批结果

模型三接口最小调用通过；初始索引覆盖 21 份文档、277 片段，收尾文档更新后重新构建。重建复用模型和正文均未变化的向量，同时刷新文件哈希与行号，不重复支付已有正文的 Embedding 调用。诊断测试 16 项、既有 HTTP 11 项、事件/导入/生命周期 13 项分别通过，共 40 个不同用例，不称为一次合并运行。

最小真实链路 `artifacts/stage5-live-u6oly0t_/` 通过实际检索、DeepSeek 报告、引用校验、SQL 精确读回、重启查询和退出清理。完整链路 `artifacts/stage5-live-vkq5420d/` 通过真实 TCP Ping、Worker 暂停导致过期事件、实际诊断、Worker 继续后采集恢复、两份历史实验、报告重启持久化、旧 running 任务中断、Python 退出与 DeepSeek 401 的故障隔离。模型正确区分 stale/null、未探测业务健康、历史实验和未核验日志；仍只是一组故障样例，不是广泛诊断效果评测。

完整检查结束后 `--hold` 因非交互 stdin EOF 进入清理；已增加 PTY 前置检查及独立报告复用入口。网页首次复用 `artifacts/stage5-live-4rubxyw6/`，发现长文本撑宽任务表、轮询折叠证据，修复后在 `artifacts/stage5-live-6iuc9slo/` 验收通过：真实网页提交并完成第二条诊断、报告与来源展开、跨轮询保持展开、断开清空、零浏览器错误、令牌无 local/session storage。桌面与 390px 窄屏 axe WCAG 2A/2AA 均零自动违规、零待检查；页面宽度与 viewport 一致，不宣称完整全站 WCAG 认证。截图和网页提交的新报告保存在该目录。

私有测试 MySQL 均通过管理员 SHUTDOWN 并 wait 后删除数据目录；HTTP、Python 和 TCP 测试实例及独立浏览器已退出，临时 TCP 配置已删。保留忽略的知识索引、参考克隆、0600 本机环境配置与验收证据。原业务数据、授权和本机 nginx.conf 未改。

正式数据库初始化、生产部署、正式监控性能开销和大规模诊断效果评测未执行；本批不提交或推送。

## 2026-10-26：最终报告输出协议更新

原阶段验收属于当时实现。当前七种取证工具请求strict=true，DeepSeek工具请求自动走beta端点；最终报告通过唯一的submit_diagnosis_report及named tool_choice返回参数，而非只依赖JSON Mode和提示词。服务端schema从Analysis派生，并以本次真实E编号限制引用；后端Pydantic保留完整约束，最多一次只修正最终报告的重生成，不重跑取证、不重试HTTP，失败仍拒绝。

API的strict支持经实测存在额外字段/非法JSON反例，不能视为强保证；output_validation记录请求模式、后端校验和修正原因。长度截断明确失败。`diagnosis.probe`的DeepSeek最小检查改为strict函数，新增独立 `--strict-contract` 验证额外字段；单次通过不代表普遍保证。细节、6次真实回放及边界见 [Strict输出记录](DIAGNOSIS_STRUCTURED_OUTPUT.md)。原3次失败保留在v1基线，未做新的全面质量评测或性能验收。

## 2026-10-26：json_schema原生端点修正

进一步核验后确认：Chat Completions的response_format=json_schema返回400，不等于DeepSeek的Responses协议不支持该格式。当前最终报告已改用 `/responses` 的 `text.format={type:json_schema,name,schema}`，完整保留Analysis长度/条数和引用enum；七种取证工具仍用原Chat循环，历史调用/结果按Responses配对格式传递。后端校验与一次最终修正保留，output_validation协议标记为deepseek_responses_json_schema。

默认diagnosis.probe同步验证Responses，新增独立 `--json-schema-contract`；原 `--strict-contract` 仅检查beta工具。4次真实诊断回放首次通过、25项相关测试通过；额外字段反例说明服务端保证仍不能过度承诺。上一节beta最终函数方案属于前一批历史状态。当前接口、400错误和验证范围见 [Responses修复](DIAGNOSIS_RESPONSES_JSON_SCHEMA.md)。

## 2026-10-26：按问题路由取代固定全量初始读取

当前先分类，再按问题取证。知识问题只检索文档并用独立知识回答schema，不访问当前指标；故障问题只选所需health/workers/events/logs和可选文档；实验问题只读指定对比或最近列表；范围外和需澄清问题不取证。分类JSON由后端校验，工具列表过滤加实际派发白名单双重约束，错误分类不触发全量读取。任务内相同工具参数不重复读，比较UUID核对原文并保留基准/候选角色。

报告保留schema_version=1，新增kind/routing；网页不再给知识回答强加根因结论。分类增加一次模型调用，仍计入已有任务期限；不能因本批声称成本或广泛诊断效果已优化。13题路由开发验收与6条固定证据回放通过；完整行为及旧报告兼容范围见 [路由说明](DIAGNOSIS_ROUTING.md)。前面固定采集描述保留为历史阶段行为。

## 2026-10-26：T4语料分层

建库保留原22份来源，使用 `diagnosis/corpus_policy.json` 审阅章节和少量混合段落的行范围。片段增加corpus元数据：当前契约/历史案例/技术原理/规划进度、发布日期（未知为null）、适用版本、组件、来源性质、替代关系及审阅版本。上游绑定导入提交；本项目标为工作树文档快照，具体版本由文件哈希识别，不冒充已发布运行版本。来源正文变化需先复核标注，检查在Embedding调用之前执行。

默认查询在BM25和Dense各取前20之前排除规划进度及已替代片段，其余参数不变。含历史、上游、旧版、路线图等明确词项的查询保留全池访问；返回retrieval_purpose及原有历史scope。历史案例和技术原理仍可参与默认查询，但不能证明当前实例事实。用途识别为本地词项规则，隐含的历史意图可能未被识别，不宣称已解决所有路由与语义问题。取证模型可看到元数据；引用支持检查仍属T12。

索引保存策略哈希；旧索引或策略不匹配需显式重建，生产路径不默默退回未分层检索。评测可用 `--legacy-corpus` 明确选择旧的全池行为，清单记录该设置；不覆盖v1冻结索引与分数。建库 `--offline` 仅复用已有向量，有新正文时在API调用前失败且保留原索引。

分类清单、完整实现与小规模检索对照的实际结果见 [T4记录](DIAGNOSIS_CORPUS_LAYERS.md)。这批验证语料分层与检索行为，不作为新版诊断准确率或正式性能验收，不改TCP、数据库、工具权限或网页。

## 2026-10-27：T5进程与信号资料主题

建库增加三份Linux man-pages摘要，涉及SIGSTOP/SIGCONT、proc状态/启动身份和waitpid停止/退出/回收语义；来源版本、日期、原始快照哈希和助手整理身份随corpus元数据返回。通用机制标为primary_reference，项目适用推导另标project_document，不将助手摘要冒充官方原文。当前受审阅来源共25份，原有参数、工具和运行指标不变。

资料与6题小规模评测已完成，具体结果、索引版本和未解决问题见 [T5记录](DIAGNOSIS_KNOWLEDGE_T5.md)。3道知识题人工核对通过；3道运行态题未检索资料，仍有机制错误或策略遗漏，不能据资料入库声称诊断质量达标。该批使用合成接口回放和真实模型，不是新的TCP故障注入；旧Worker暂停案例仅复核历史证据。

## 2026-10-28：T11/T12证据计划与支持校验

当前先分类、按题读取运行/历史证据，再按问题和已取数据补充采样、Worker状态、整体退出策略、事件、实验可比性及能力边界中的必要契约。契约来自固定的 `diagnosis/observation_contracts.json`，核对其引用源码/文档哈希，失败时仅报告观测与未知，不拿模型记忆填补；它是内部只读资料查表，不是新开放的运行工具。普通有效计数/监控存活查询直接回答观测，不运行额外Agent选工具循环、不强填未知或根因。

新报告analysis_schema_version=2。facts/possible_causes/excluded_causes每项声明basis和support，使用该E编号原始JSON的RFC6901指针与原值、显式length数量，或原样短摘录。检查类型、引用集合、可见值/数量/摘录、部分数字/完整标识、Worker实体混用、来源范围及少量已知项目矛盾；历史记录正文和未核验用户/日志不能冒充当前观测。本次查询可用性、记录数量和detection控制面摘要与历史事件内容分别处理。程序并不理解所有自然语言，semantic_status保持requires_review。

生成错误共享一次最终修正，不重取证、不执行建议。支持/格式或远端失败任务可保存kind=failure、delivery_status=rejected的元数据与有界证据，状态仍为失败或超时，不展示成功事实。成功与失败均记录提示词/代码/知识索引哈希、检索过程、实际模型标识/缺失、用量/缺失及结束/未完成状态；检索上下文与缓存按任务隔离，取证时间不因复用刷新。嵌套JSON脱敏保持结构与幂等，原值定位不会因落库前再次脱敏被破坏。

外层schema_version=1及数据库JSON存储保持，旧记录仍可读；网页标明来源类别、可排除解释、失败记录和报告生成时间，数据/事件时点仍以来源为准。工作树契约不等于对运行二进制版本的独立验证。完整实现、人工可复查的源审阅流程、代价对照、拒绝结果与实际验收范围见 [T11/T12交付](DIAGNOSIS_EVIDENCE_POLICY.md)，不宣称全语义自动证明或新真实TCP故障复测。
