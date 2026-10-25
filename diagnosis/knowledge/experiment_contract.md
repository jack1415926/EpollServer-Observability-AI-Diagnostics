# 当前实验与比较契约

适用：本仓库monitor_experiment报告和对比API。依据monitor/http/experiments.cxx及testscript/import_experiment.py；这里没有新增性能测量。

## 历史实验与因果

query_experiments列出已存储的历史摘要，get_experiment按UUID读取详情，compare_experiments比较两条已有记录。历史结果不代表当前实例状态。same_conditions只表示已记录条件相同，不证明整个环境完全相同；负载、构建、配置、版本或环境不同，不可将QPS差异直接归因于优化。

## 缺失统计与零基准

P95是分位点，不能唯一确定均值。旧报告均值缺失应保留null，不能由P50/P95推算，也不能认定抽样模式必然没有均值；新报告的mean可为已采集延迟样本均值。

相对变化=(候选-基准)/基准*100。基准为0时相对变化未定义，接口返回null；若两者均为0，绝对差值仍为0。这两个结论不冲突，不能把相对变化未定义说成绝对值也不知道。

## 原始来源和受限工具

详情中的context、文件哈希和resources摘要保留原实验条件，不把旧结果改写为新测量。当前工具不检索任意文件，也不执行压测。需要原始报告时由操作者明确提供目录，验收脚本不能依赖开发者机器上的固定artifacts路径。
