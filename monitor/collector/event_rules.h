#pragma once
#include <nlohmann/json.hpp>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace epoll_monitor {
struct EventConfig {
    uint64_t trigger_ms = 3000, recovery_ms = 3000;
    uint64_t receive_threshold = 1024, send_threshold = 1024;
};

// A single owner supplies monotonic time; missing evidence resets continuity.
class EventRules {
    struct State {
        std::optional<uint64_t> since;
        bool bad = false;
        nlohmann::json active;
    };
    EventConfig config_;
    std::string instance_;
    std::function<std::string()> id_;
    std::map<std::string, State> states_;
    std::optional<uint64_t> previous_;
    bool stopping_ = false;
public:
    EventRules(EventConfig config, std::string instance, std::function<std::string()> id);
    void Restore(const nlohmann::json &event);
    void ResetWindows();
    std::vector<nlohmann::json> Observe(const nlohmann::json &sample, uint64_t now, int64_t unixMs);
};
}
