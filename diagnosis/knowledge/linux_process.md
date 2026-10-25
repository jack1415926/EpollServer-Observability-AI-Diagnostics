# Linux进程观测原理

来源：Linux man-pages proc_pid_stat(5)，2026-10-25核对在线版本。
https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html
线程范围另核对proc_pid_task(5)及Linux v6.17的/proc实现；这是语义参考，不声称与本机7.0内核源码逐字相同。
https://man7.org/linux/man-pages/man5/proc_pid_task.5.html
https://raw.githubusercontent.com/torvalds/linux/v6.17/fs/proc/array.c

## 状态与访问

/proc/PID/stat含状态、父PID和启动刻度；同一PID可能被复用，不能仅按PID关联。T是停止，t是跟踪停止；暂停不等于/proc文件必然不可读。多线程进程的主线程状态也不能代表每个线程的忙闲。

## CPU与RSS

utime/stime用clock ticks计累计CPU时间，按系统tick频率换算；累计量不等于当前利用率。RSS字段按页计且是近似值，按实际页大小换算字节，不称为精确内存。等待I/O也不等同于持续占用CPU。

适用限制：Linux进程身份与基础资源解释，不证明本项目当前存在CPU瓶颈或内存泄漏，也不据此认定某个线程停止更新业务快照的完整原因。
