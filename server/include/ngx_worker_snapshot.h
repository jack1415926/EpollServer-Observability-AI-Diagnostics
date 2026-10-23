#ifndef NGX_WORKER_SNAPSHOT_H
#define NGX_WORKER_SNAPSHOT_H

#include <condition_variable>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <thread>

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

#endif
