#ifndef EPOLL_TCP_PROBE_H
#define EPOLL_TCP_PROBE_H

#include <condition_variable>
#include <functional>
#include <mutex>
#include <thread>
#include <nlohmann/json.hpp>

namespace epoll_monitor {
struct TcpProbeConfig {
    unsigned port = 0, intervalMs = 5000, timeoutMs = 1000;
};
TcpProbeConfig ReadTcpProbeConfig();
nlohmann::json ProbeTcpOnce(const TcpProbeConfig &config, const nlohmann::json &master);

class TcpProbe {
    TcpProbeConfig config_;
    std::mutex mutex_;
    std::condition_variable wake_;
    bool stop_ = false;
    nlohmann::json value_;
    uint64_t completed_ = 0;
    std::thread thread_;
public:
    TcpProbe(TcpProbeConfig config, std::function<nlohmann::json()> sample);
    ~TcpProbe();
    nlohmann::json Read(const nlohmann::json &master);
};
}
#endif
