# Linux进程状态：T、Z与PID启动身份

来源：Linux man-pages `proc_pid_stat(5)`，本机6.17原始手册；页面日期2026-02-08，核对日期2026-10-27。
一手入口：[proc_pid_stat(5)](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html)。在线6.19相关字段交叉核对；本文是项目助手要点整理，不是官方原文或运行观测。

## proc stat字段能说明什么

- `/proc/PID/stat`第3字段state描述进程状态。现代Linux中T表示信号导致的停止，t表示跟踪停止，Z表示僵尸；T与Z不能互换。
- 第22字段starttime是进程启动时刻相对于系统启动的时钟滴答数，不是Unix毫秒时间。PID存在不足以排除PID复用。
- 核对进程身份应结合系统启动轮次、PID和starttime；这些字段也不证明业务线程正常处理请求。

## 本项目公开数据的边界

当前C++读取器内部读取state、ppid和starttime，并把Z/X/x拒绝为dead；它没有通过Worker API直接返回T/t或原始state。诊断模型不能把工具中的stale自动翻译成已观测到T。

stale是快照年龄状态；process_unavailable是进程信息不可读；二者都不是确定的退出原因。需要额外核验时，建议由人工按启动身份和时间窗口取证，不能声称现有工具已读到未暴露字段。实现见[读取器](../../monitor/collector/collector.cxx)。
