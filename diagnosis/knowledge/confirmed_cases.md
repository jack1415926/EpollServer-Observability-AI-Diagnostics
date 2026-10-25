# 经测试确认的项目历史案例

资料类型为历史案例，不是当前实例故障。依据fbebd4f所包含的修复、docs/DEVELOPMENT_ISSUES.md与相应真实实现测试。案例正文不吸收模型生成报告。

## CASE-LOG-FORMAT：日志格式导致Worker崩溃

环境：本项目自定义日志格式化器与MySQL注册路径。现象：特定SQL失败日志可能导致Worker崩溃。已确认原因：使用标准%u而自定义格式化器未消费对应参数，后续%s把数字误作地址；已改为%ud。另一处二次记录把已格式化文本再次当格式串，改为固定%s传递。

区分依据：包装SQL失败码1062/2006，连接真实DAO、日志和格式化器复现；修复后原样保留错误文本及百分号。验证入口：testscript/test_registration_logging.cxx、testscript.test_service_fixes。未知边界：未穷举所有日志错误路径，不由当前Worker退出直接断言同一根因。

## CASE-SNAPSHOT-ID：身份状态混用造成错误告警

环境：快照读取器与阶段4事件规则。现象：快照中的身份字段错误曾与实际进程身份不符共用identity_mismatch，可能误报进程异常。修复：单列snapshot_identity_mismatch；只把真实进程身份或父进程不符作为进程异常证据。

区分依据：使用生产读取器和生产状态机组合测试，快照字段错误只触发采集事件，实际进程启动身份错误仍触发进程事件。验证入口：testscript.test_metrics_collector、testscript.test_event_rules。该历史案例不证明当前文件损坏或PID被复用。

## CASE-TEST-LIFETIME：测试数据库目录删除过早

环境：私有MySQL验收脚本。现象：退出未确认时删除目录曾导致残留实例继续写错误日志。修复：管理员SHUTDOWN、wait确认退出后才删除；失败保留目录并报告身份。初始化进程也纳入资源跟踪，独立清理用ExitStack保证各资源都尝试退出。

验证入口：testscript.test_stage3_lifecycle、私有数据库预检及阶段3/4/5实际清理记录。未知：沙箱初始化卡住的底层原因未查明，不将其记为确定的MySQL产品故障；本案例不代表当前业务数据库异常。
