# main整合记录

用户要求将T7及此前诊断成果合并到main。整合父版本为main的`2177e70`（2026-10-25 17:05，UTC+8）和T7分支`eb833ae`（2026-10-28 23:28，UTC+8）。先前`5143efb`只是本地检查点，之后通过GitHub连接发布在独立分支；没有更新main，因此main显示三天前是实际分支状态，不是今天成果丢失。本次以两个父提交的合并提交更新main，不改写旧历史。

## 冲突选择与兼容边界

| 范围 | 整合行为 |
|---|---|
| Worker资源 | 保留T7 `resources`的CPU窗口百分比、RSS和独立可用性；保留main `process_resources`的已核验主线程状态、累计CPU ticks、HZ、线程数和近似RSS。两组来自同一/proc身份核验，不增加第二套采样线程 |
| 页面 | 保留CPU百分比/RSS/采样窗口，同时显示主线程状态、线程数和累计CPU；滚动区继续可键盘聚焦 |
| 诊断 | 保留路由、原生Responses、证据支持检查和任务隔离；保留main的UUID实验详情工具，detail路由只能读取问题中唯一UUID，不能从列表擅选 |
| 知识库 | 默认`--corpus current`使用受审阅的分层文档；main的六份知识目录作为显式`--corpus catalog`保留，`--corpus legacy`保留旧文档回放。不是把两套语料混在同一默认候选池 |
| 固定索引 | 保留`EPOLL_DIAG_INDEX_PATH`；catalog索引绑定自身目录哈希。旧版本的catalog索引须按catalog模式重建，不放宽当前生产索引的策略验证 |
| 评测 | 两套原始问题、标签和审阅记录保留。main的重名测试移到`test_legacy_diagnosis_evaluation.py`，旧评测增加显式`--legacy-index`兼容冻结索引，并按当前生产候选过滤做排名对照。旧成绩不重算、不回填、不冒充新盲测 |
| 测试资源 | 保留完整联调必须显式传`--reports`且启动前验证的入口；原本机nginx.conf、依赖、凭据和原始产物不提交 |

主线程状态不等于所有业务线程状态，累计CPU不等于当前百分比；T7原范围中的逐线程/请求分析仍未完成。T11/T12残余语义问题继续保留，合并不代表诊断质量达标。

```bash
# 默认当前语料
.venv/bin/python -m diagnosis.retrieval
# 显式构建保留的知识目录；建库会调用Embedding，旧索引不覆盖原成绩
.venv/bin/python -m diagnosis.retrieval --corpus catalog --output artifacts/catalog-merged/index.npz
# 旧质量集仅显式读取冻结旧索引，不自动重做付费生成
.venv/bin/python -m evaluation.diagnosis.run --help
```

## 验证与交付边界

先执行旧main知识目录/实验详情/评测完整性及新路由组合回归，再核验新旧Worker资源字段、真实/proc停止/恢复和HTTP行为。合并中新测试的导入遗漏在最小用例定位并修正，不当作生产故障。最终结果及代码/索引/日志哈希写入`evaluation/main_merge_20261029/acceptance.json`；原T7与质量评测证据不覆盖。

本次不做大规模压测、付费模型质量评测、数据库迁移或生产部署。服务测试沿用有界监督及沙箱外执行；进程只按本次归属退出，数据目录只在退出确认后清理。
