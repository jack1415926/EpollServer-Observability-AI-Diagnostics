#include "ngx_worker_snapshot.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <fcntl.h>
#include <fstream>
#include <pthread.h>
#include <signal.h>
#include <sstream>
#include <stdexcept>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

extern void ngx_log_error_core(int level, int err, const char *fmt, ...);

namespace {
uint64_t StartTicks(int pid)
{
    std::ifstream input("/proc/" + std::to_string(pid) + "/stat");
    std::string line;
    std::getline(input, line);
    const auto end = line.rfind(')'); // comm can contain spaces and parentheses.
    if (end == std::string::npos)
        throw std::runtime_error("missing process identity");
    std::istringstream fields(line.substr(end + 1));
    std::string value;
    for (int field = 3; field <= 22; ++field)
        if (!(fields >> value))
            throw std::runtime_error("invalid process identity");
    return std::stoull(value);
}

std::string BootId()
{
    std::ifstream boot("/proc/sys/kernel/random/boot_id");
    std::string value;
    std::getline(boot, value);
    if (value.size() != 36 || value.find_first_not_of("0123456789abcdef-") != std::string::npos)
        throw std::runtime_error("invalid boot identity");
    return value;
}

int OpenDirectory(const std::string &directory)
{
    if (mkdir(directory.c_str(), 0700) == -1 && errno != EEXIST)
        throw std::runtime_error("cannot create snapshot directory");
    const int fd = open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct stat info;
    if (fd == -1 || fstat(fd, &info) == -1 || info.st_uid != geteuid() ||
        (info.st_mode & (S_IWGRP | S_IWOTH))) {
        if (fd != -1)
            close(fd);
        throw std::runtime_error("snapshot directory must be owned and not group/world writable");
    }
    return fd;
}

bool WriteSnapshot(int directoryFd, const std::string &filename, const std::string &data)
{
    const std::string temporary = filename + ".tmp";
    const int fd = openat(directoryFd, temporary.c_str(),
                          O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd == -1)
        return false;
    size_t offset = 0;
    while (offset < data.size()) {
        const ssize_t written = write(fd, data.data() + offset, data.size() - offset);
        if (written == -1 && errno == EINTR)
            continue;
        if (written <= 0)
            break;
        offset += static_cast<size_t>(written);
    }
    const bool closed = close(fd) == 0;
    if (offset == data.size() && closed &&
        renameat(directoryFd, temporary.c_str(), directoryFd, filename.c_str()) == 0)
        return true;
    unlinkat(directoryFd, temporary.c_str(), 0);
    return false;
}

}

bool CWorkerSnapshot::Start(const std::string &directory, int workerIndex,
                            std::function<SWorkerMetrics()> readMetrics)
{
    if (m_directoryFd != -1 || m_thread.joinable())
        return false;
    try {
        m_pid = getpid();
        m_masterPid = getppid();
        m_workerIndex = workerIndex;
        m_startTicks = StartTicks(m_pid);
        m_masterStartTicks = StartTicks(m_masterPid);
        m_bootId = BootId();
        m_filename = "worker-" + m_bootId + "-" + std::to_string(m_pid) +
                     "-" + std::to_string(m_startTicks) + ".json";
        m_directoryFd = OpenDirectory(directory);
        m_readMetrics = std::move(readMetrics);
        m_stopping = false;
        m_sequence = m_writeFailures = 0;
        // Only the Worker's event thread should receive process shutdown signals.
        sigset_t blocked, previous;
        sigfillset(&blocked);
        const int error = pthread_sigmask(SIG_BLOCK, &blocked, &previous);
        if (error)
            throw std::runtime_error("cannot block publisher signals");
        try {
            m_thread = std::thread(&CWorkerSnapshot::Run, this);
        } catch (...) {
            pthread_sigmask(SIG_SETMASK, &previous, nullptr);
            throw;
        }
        pthread_sigmask(SIG_SETMASK, &previous, nullptr);
        return true;
    } catch (const std::exception &error) {
        ngx_log_error_core(5, 0, "Worker snapshot publisher initialization failed; TCP continues (%s)", error.what());
        // No publication exists yet; never unlink files in a rejected directory.
        if (m_directoryFd != -1) {
            close(m_directoryFd);
            m_directoryFd = -1;
        }
        return false;
    }
}

void CWorkerSnapshot::Stop()
{
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_stopping = true;
    }
    m_wakeup.notify_one();
    if (m_thread.joinable())
        m_thread.join();
    if (m_directoryFd != -1) {
        unlinkat(m_directoryFd, m_filename.c_str(), 0);
        unlinkat(m_directoryFd, (m_filename + ".tmp").c_str(), 0);
        close(m_directoryFd);
        m_directoryFd = -1;
    }
}

void CWorkerSnapshot::Run()
{
    bool failed = false;
    for (;;) {
        bool success = false;
        try {
            success = Publish();
        } catch (const std::exception &) {
            // Allocation/formatting failures must not terminate the TCP process.
        }
        if (!success) {
            ++m_writeFailures;
            if (!failed)
                ngx_log_error_core(5, 0, "Worker snapshot publication failed; TCP continues");
        } else if (failed) {
            ngx_log_error_core(6, 0, "Worker snapshot publication recovered");
        }
        failed = !success;
        std::unique_lock<std::mutex> lock(m_mutex);
        if (m_wakeup.wait_for(lock, std::chrono::seconds(1), [this] { return m_stopping; }))
            break;
    }
}

bool CWorkerSnapshot::Publish()
{
    const auto metrics = m_readMetrics();
    struct timespec bootTime;
    if (clock_gettime(CLOCK_BOOTTIME, &bootTime) == -1)
        return false;
    const auto unixMs = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
    std::ostringstream json;
    json << "{\"schema_version\":1,\"boot_id\":\"" << m_bootId
         << "\",\"master_pid\":" << m_masterPid
         << ",\"master_start_ticks\":" << m_masterStartTicks
         << ",\"worker_index\":" << m_workerIndex << ",\"pid\":" << m_pid
         << ",\"process_start_ticks\":" << m_startTicks
         << ",\"sampled_at_unix_ms\":" << unixMs
         << ",\"sampled_at_boottime_ms\":" << (bootTime.tv_sec * 1000LL + bootTime.tv_nsec / 1000000)
         << ",\"sequence\":" << ++m_sequence
         << ",\"collection_ok\":true,\"write_failures_total\":" << m_writeFailures
         << ",\"connections_current\":" << metrics.connections
         << ",\"receive_queue_messages\":" << metrics.receive_queue
         << ",\"send_queue_messages\":" << metrics.send_queue << "}\n";
    return WriteSnapshot(m_directoryFd, m_filename, json.str());
}

bool CMasterSnapshot::Start(const std::string &directory,
                            const std::vector<std::pair<int, int>> &workers, int expectedWorkers)
{
    if (m_directoryFd != -1)
        return false;
    try {
        m_pid = getpid();
        m_startTicks = StartTicks(m_pid);
        m_bootId = BootId();
        m_expectedWorkers = expectedWorkers;
        m_workers.clear();
        for (const auto &worker : workers)
            m_workers.push_back({worker.first, worker.second, StartTicks(worker.second)});
        m_filename = "master-" + m_bootId + "-" + std::to_string(m_pid) +
                     "-" + std::to_string(m_startTicks) + ".json";
        m_directoryFd = OpenDirectory(directory);
        return true;
    } catch (const std::exception &error) {
        ngx_log_error_core(5, 0, "Master snapshot publisher initialization failed; TCP continues (%s)", error.what());
        return false;
    }
}

void CMasterSnapshot::Update(const std::vector<pid_t> &activePids, bool stopping)
{
    if (m_directoryFd == -1)
        return;
    bool success = false;
    try {
        std::ostringstream json;
        json << "{\"schema_version\":1,\"boot_id\":\"" << m_bootId
             << "\",\"master_pid\":" << m_pid << ",\"master_start_ticks\":" << m_startTicks
             << ",\"sequence\":" << ++m_sequence
             << ",\"write_failures_total\":" << m_writeFailures
             << ",\"state\":\"" << (stopping ? "stopping" : "running")
             << "\",\"expected_workers\":" << m_expectedWorkers << ",\"workers\":[";
        bool first = true;
        for (const auto &worker : m_workers) {
            if (std::find(activePids.begin(), activePids.end(), worker.pid) == activePids.end())
                continue;
            if (!first)
                json << ',';
            first = false;
            json << "{\"worker_index\":" << worker.index << ",\"pid\":" << worker.pid
                 << ",\"process_start_ticks\":" << worker.startTicks << '}';
        }
        json << "]}\n";
        success = WriteSnapshot(m_directoryFd, m_filename, json.str());
    } catch (const std::exception &) {
        // Observability allocation/formatting failure must not change fail-stop policy.
    }
    if (!success) {
        ++m_writeFailures;
        if (!m_failed)
            ngx_log_error_core(5, 0, "Master snapshot publication failed; TCP continues");
    } else if (m_failed) {
        ngx_log_error_core(6, 0, "Master snapshot publication recovered");
    }
    m_failed = !success;
}

CMasterSnapshot::~CMasterSnapshot()
{
    if (m_directoryFd != -1) {
        unlinkat(m_directoryFd, m_filename.c_str(), 0);
        unlinkat(m_directoryFd, (m_filename + ".tmp").c_str(), 0);
        close(m_directoryFd);
    }
}
