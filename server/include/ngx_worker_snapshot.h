#ifndef NGX_WORKER_SNAPSHOT_H
#define NGX_WORKER_SNAPSHOT_H

#include <condition_variable>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <sys/types.h>
#include <thread>
#include <utility>
#include <vector>

struct SWorkerMetrics
{
    int connections;
    int receive_queue;
    int send_queue;
};

// One publisher per Worker, created only after fork and stopped before teardown.
class CWorkerSnapshot
{
public:
    ~CWorkerSnapshot() { Stop(); }
    bool Start(const std::string &directory, int workerIndex,
               std::function<SWorkerMetrics()> readMetrics);
    void Stop();

private:
    void Run();
    bool Publish();
    int m_directoryFd = -1;
    int m_pid = 0;
    int m_masterPid = 0;
    int m_workerIndex = 0;
    uint64_t m_startTicks = 0;
    uint64_t m_masterStartTicks = 0;
    uint64_t m_sequence = 0;
    uint64_t m_writeFailures = 0;
    std::string m_bootId;
    std::string m_filename;
    std::function<SWorkerMetrics()> m_readMetrics;
    std::thread m_thread;
    std::mutex m_mutex;
    std::condition_variable m_wakeup;
    bool m_stopping = false;
};

// Lifecycle membership only: a forked Worker is not necessarily ready or healthy.
class CMasterSnapshot
{
public:
    CMasterSnapshot() = default;
    CMasterSnapshot(const CMasterSnapshot &) = delete;
    CMasterSnapshot &operator=(const CMasterSnapshot &) = delete;
    ~CMasterSnapshot();
    bool Start(const std::string &directory, const std::vector<std::pair<int, int>> &workers,
               int expectedWorkers);
    void Update(const std::vector<pid_t> &activePids, bool stopping);

private:
    struct Worker { int index; int pid; uint64_t startTicks; };
    std::vector<Worker> m_workers;
    int m_directoryFd = -1;
    int m_pid = 0;
    int m_expectedWorkers = 0;
    uint64_t m_startTicks = 0;
    uint64_t m_sequence = 0;
    uint64_t m_writeFailures = 0;
    bool m_failed = false;
    std::string m_bootId;
    std::string m_filename;
};

#endif
