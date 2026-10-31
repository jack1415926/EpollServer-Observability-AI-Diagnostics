#include "tcp_probe.h"
#include <arpa/inet.h>
#include <array>
#include <charconv>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <filesystem>
#include <poll.h>
#include <set>
#include <sstream>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>

namespace epoll_monitor {
namespace {
using Json = nlohmann::json;
using Clock = std::chrono::steady_clock;
uint64_t BootMs()
{
    timespec now{};
    if (clock_gettime(CLOCK_BOOTTIME, &now)) throw std::runtime_error("clock_unavailable");
    return uint64_t(now.tv_sec) * 1000 + now.tv_nsec / 1000000;
}
int64_t UnixMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}
Json Result(const TcpProbeConfig &config, const char *status, const char *reason, const Json &master)
{
    return {{"status", status}, {"reason", reason}, {"rtt_ms", nullptr}, {"checked_at_unix_ms", nullptr},
            {"age_ms", nullptr}, {"host", "127.0.0.1"}, {"port", config.port ? Json(config.port) : Json(nullptr)},
            {"interval_ms", config.intervalMs}, {"timeout_ms", config.timeoutMs},
            {"max_age_ms", 2 * config.intervalMs + config.timeoutMs}, {"instance", master.is_object() ?
                Json{{"boot_id", master.at("boot_id")}, {"master_pid", master.at("master_pid")},
                     {"master_start_ticks", master.at("master_start_ticks")}} : Json(nullptr)}};
}
bool Identity(const Json &master)
{
    if (!master.is_object() || master.value("state", "") != "running") return false;
    std::string boot, stat;
    std::ifstream("/proc/sys/kernel/random/boot_id") >> boot;
    std::getline(std::ifstream("/proc/" + std::to_string(master.at("master_pid").get<int>()) + "/stat"), stat);
    const auto end = stat.rfind(')');
    if (end == std::string::npos || boot != master.at("boot_id")) return false;
    std::istringstream fields(stat.substr(end + 1));
    std::string field;
    for (int i = 0; i <= 19; ++i) {
        if (!(fields >> field) || (i == 0 && (field == "Z" || field == "X" || field == "x"))) return false;
    }
    return std::stoull(field) == master.at("master_start_ticks").get<uint64_t>();
}
// A matching listener must be an FD of the selected Master, not merely a local open port.
const char *Listener(const Json &master, unsigned port)
{
    const std::string proc = "/proc/" + std::to_string(master.at("master_pid").get<int>());
    std::set<std::string> sockets;
    for (const auto &fd : std::filesystem::directory_iterator(proc + "/fd")) {
        std::error_code error;
        const auto target = std::filesystem::read_symlink(fd.path(), error).string();
        if (!error && target.rfind("socket:[", 0) == 0)
            sockets.insert(target.substr(8, target.size() - 9));
    }
    std::ifstream tcp(proc + "/net/tcp");
    if (!tcp) throw std::runtime_error("target_unavailable");
    bool matched = false;
    std::string line;
    std::getline(tcp, line);
    while (std::getline(tcp, line)) {
        std::istringstream fields(line);
        std::array<std::string, 10> values;
        bool complete = true;
        for (auto &value : values) if (!(fields >> value)) { complete = false; break; }
        if (!complete || values[3] != "0A") continue;
        const auto colon = values[1].find(':');
        if (colon == std::string::npos || std::stoul(values[1].substr(colon + 1), nullptr, 16) != port) continue;
        const auto address = values[1].substr(0, colon);
        if (address != "00000000" && address != "0100007F") continue;
        matched = true;
        if (sockets.count(values[9])) return "owned";
    }
    return matched ? "target_mismatch" : "not_listening";
}
struct Socket { int fd; ~Socket() { if (fd >= 0) close(fd); } };
void Wait(int fd, short events, Clock::time_point deadline)
{
    for (;;) {
        const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now()).count();
        if (left <= 0) throw std::runtime_error("timeout");
        pollfd item{fd, events, 0};
        const int result = poll(&item, 1, static_cast<int>(left));
        if (result > 0) return;
        if (result < 0 && errno == EINTR) continue;
        throw std::runtime_error(result == 0 ? "timeout" : "socket_error");
    }
}
double Ping(unsigned port, unsigned timeout)
{
    const auto started = Clock::now(), deadline = started + std::chrono::milliseconds(timeout);
    Socket socket{::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0)};
    if (socket.fd < 0) throw std::runtime_error("socket_error");
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons(port);
    if (connect(socket.fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)) < 0) {
        if (errno != EINPROGRESS) throw std::runtime_error("connect_failed");
        Wait(socket.fd, POLLOUT, deadline);
        int error = 0;
        socklen_t length = sizeof(error);
        if (getsockopt(socket.fd, SOL_SOCKET, SO_ERROR, &error, &length) < 0 || error)
            throw std::runtime_error("connect_failed");
    }
    const std::array<unsigned char, 8> packet{0, 8, 0, 0, 0, 0, 0, 0};
    std::array<unsigned char, 8> response{};
    for (bool sending : {true, false}) {
        size_t offset = 0;
        while (offset < packet.size()) {
            Wait(socket.fd, sending ? POLLOUT : POLLIN, deadline);
            const auto count = sending ? send(socket.fd, packet.data() + offset, packet.size() - offset, MSG_NOSIGNAL) :
                recv(socket.fd, response.data() + offset, response.size() - offset, 0);
            if (count < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
            if (count <= 0) throw std::runtime_error(count == 0 ? "connection_closed" : "socket_error");
            offset += count;
        }
    }
    if (response != packet) throw std::runtime_error("protocol_error");
    return std::chrono::duration<double, std::milli>(Clock::now() - started).count();
}
}

TcpProbeConfig ReadTcpProbeConfig()
{
    TcpProbeConfig config;
    for (auto field : {std::pair{"EPOLL_MONITOR_TCP_PORT", &config.port},
                       {"EPOLL_MONITOR_TCP_INTERVAL_MS", &config.intervalMs},
                       {"EPOLL_MONITOR_TCP_TIMEOUT_MS", &config.timeoutMs}}) {
        if (const char *raw = std::getenv(field.first)) {
            const std::string text(raw);
            const auto parsed = std::from_chars(text.data(), text.data() + text.size(), *field.second);
            if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() || *field.second == 0)
                throw std::invalid_argument("invalid_tcp_probe_config");
        }
    }
    if (config.port > 65535 || config.intervalMs < 100 || config.intervalMs > 60000 ||
        config.timeoutMs < 10 || config.timeoutMs > 5000 || config.timeoutMs > config.intervalMs)
        throw std::invalid_argument("invalid_tcp_probe_config");
    return config;
}

Json ProbeTcpOnce(const TcpProbeConfig &config, const Json &master)
{
    Json result = Result(config, config.port ? "unknown" : "not_probed", config.port ? "initializing" : "disabled", master);
    if (!config.port) return result;
    try {
        if (!Identity(master)) { result["reason"] = "identity_unavailable"; return result; }
        const std::string listener = Listener(master, config.port);
        if (listener != "owned") {
            result["status"] = listener == "not_listening" ? "unreachable" : "unknown";
            result["reason"] = listener;
        } else {
            result["status"] = "reachable";
            result["reason"] = "ping_ok";
            try { result["rtt_ms"] = Ping(config.port, config.timeoutMs); }
            catch (const std::runtime_error &error) {
                result["status"] = "unreachable"; result["reason"] = error.what();
            }
        }
        if (!Identity(master)) { result["status"] = "unknown"; result["reason"] = "identity_changed"; result["rtt_ms"] = nullptr; }
        else if (result["status"] == "reachable" && std::string(Listener(master, config.port)) != "owned") {
            result["status"] = "unknown"; result["reason"] = "target_mismatch"; result["rtt_ms"] = nullptr;
        }
        result["checked_at_unix_ms"] = UnixMs();
    } catch (const std::exception &) {
        result["status"] = "unknown"; result["reason"] = "target_unavailable"; result["rtt_ms"] = nullptr;
    }
    return result;
}

TcpProbe::TcpProbe(TcpProbeConfig config, std::function<Json()> sample)
    : config_(config), value_(Result(config, config.port ? "unknown" : "not_probed", config.port ? "initializing" : "disabled", nullptr))
{
    if (!config_.port) return;
    thread_ = std::thread([this, sample] {
        for (;;) {
            Json result;
            try { result = ProbeTcpOnce(config_, sample().value("master", Json(nullptr))); }
            catch (const std::exception &) { result = Result(config_, "unknown", "collection_error", nullptr); }
            std::unique_lock<std::mutex> lock(mutex_);
            value_ = std::move(result); completed_ = BootMs();
            if (wake_.wait_for(lock, std::chrono::milliseconds(config_.intervalMs), [this] { return stop_; })) return;
        }
    });
}
TcpProbe::~TcpProbe()
{
    { std::lock_guard<std::mutex> lock(mutex_); stop_ = true; }
    wake_.notify_one();
    if (thread_.joinable()) thread_.join();
}
Json TcpProbe::Read(const Json &master)
{
    std::lock_guard<std::mutex> lock(mutex_);
    Json result = value_;
    if (!config_.port) return result;
    const Json selected = Result(config_, "unknown", "identity_changed", master)["instance"];
    if (selected.is_null() || selected != result["instance"])
        return Result(config_, "unknown", "identity_changed", master);
    const auto age = completed_ ? BootMs() - completed_ : 0;
    result["age_ms"] = completed_ ? Json(age) : Json(nullptr);
    if (age >= 2 * config_.intervalMs + config_.timeoutMs) {
        result["status"] = "unknown"; result["reason"] = "stale"; result["rtt_ms"] = nullptr;
    }
    return result;
}
}
