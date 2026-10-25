#include "event_rules.h"
#include <set>

namespace epoll_monitor {
using J = nlohmann::json;
EventRules::EventRules(EventConfig config, std::string instance, std::function<std::string()> id)
    : config_(config), instance_(std::move(instance)), id_(std::move(id)) {}

void EventRules::Restore(const J &event)
{
    if (event.at("instance") != instance_ || event.at("status") != "active") return;
    for (const char *field : {"id", "key", "rule"})
        if (!event.at(field).is_string() || event.at(field).get<std::string>().empty())
            throw std::runtime_error("invalid_stored_event");
    if (!event.at("triggered_at_unix_ms").is_number_integer() ||
        !event.at("recovery_window_ms").is_number_unsigned() ||
        event.at("recovery_window_ms").get<uint64_t>() == 0 || !event.contains("observed") ||
        !event.contains("worker") || !event.contains("threshold")) throw std::runtime_error("invalid_stored_event");
    const std::string rule = event.at("rule");
    if ((rule == "receive_queue_backlog" || rule == "send_queue_backlog") &&
        (!event.at("threshold").is_number_unsigned() || event.at("threshold").get<uint64_t>() == 0))
        throw std::runtime_error("invalid_stored_threshold");
    auto &state = states_[event.at("key").get<std::string>()];
    if (!state.active.is_null()) throw std::runtime_error("duplicate_active_event");
    state.active = event;
}

void EventRules::ResetWindows()
{
    for (auto &item : states_) item.second.since.reset();
    previous_.reset();
}

std::vector<J> EventRules::Observe(const J &sample, uint64_t now, int64_t unixMs)
{
    // A suspended detector has no evidence for the time it did not observe.
    if (previous_ && (now < *previous_ || now - *previous_ > 1500)) ResetWindows();
    previous_ = now;
    std::vector<J> changes;
    std::set<std::string> seen;
    auto observe = [&](const std::string &key, const std::string &rule, std::optional<bool> bad,
                       const J &observed, const J &threshold, const J &worker) {
        seen.insert(key);
        if (!states_.count(key) && states_.size() >= 12289) throw std::runtime_error("event_rule_capacity");
        auto &s = states_[key];
        if (!bad) { s.since.reset(); return; }
        if (!s.since || s.bad != *bad) { s.since = now; s.bad = *bad; }
        const auto window = *bad ? config_.trigger_ms : (s.active.is_null() ? config_.recovery_ms :
                                                        s.active.at("recovery_window_ms").get<uint64_t>());
        if (now - *s.since < window) return;
        if (*bad && s.active.is_null()) {
            s.active = {{"id", id_()}, {"instance", instance_}, {"key", key}, {"rule", rule},
                {"worker", worker}, {"status", "active"}, {"triggered_at_unix_ms", unixMs},
                {"condition_since_unix_ms", unixMs - static_cast<int64_t>(now - *s.since)},
                {"resolved_at_unix_ms", nullptr}, {"observed", observed}, {"recovery_observed", nullptr},
                {"threshold", threshold}, {"trigger_window_ms", config_.trigger_ms},
                {"recovery_window_ms", config_.recovery_ms}};
            changes.push_back(s.active);
        } else if (!*bad && !s.active.is_null()) {
            s.active["status"] = "resolved";
            s.active["resolved_at_unix_ms"] = unixMs;
            s.active["recovery_observed"] = observed;
            changes.push_back(s.active);
            s.active = nullptr;
        }
    };
    const auto status = sample.at("status").get<std::string>();
    if (status == "stopping") stopping_ = true;
    else if (status != "instance_changed" && !sample.at("master").is_null() &&
             sample.at("master").at("state") == "running") stopping_ = false;
    const bool stopping = stopping_;
    const bool good = status == "complete";
    std::optional<bool> collection;
    if (!stopping && sample.at("reason") != "initializing") collection = !good;
    observe("collection", "collection_unavailable", collection,
            {{"status", status}, {"reason", sample.at("reason")}}, "all_expected_workers_fresh", nullptr);
    if (!stopping && (status == "complete" || status == "partial" || !sample.at("master").is_null())) {
        for (const auto &row : sample.at("workers")) {
            const J identity = {{"pid", row.at("pid")}, {"process_start_ticks", row.at("process_start_ticks")},
                                {"worker_index", row.at("worker_index")}};
            const auto suffix = row.at("pid").dump() + ":" + row.at("process_start_ticks").dump();
            const std::string ws = row.at("status");
            const bool fresh = ws == "fresh";
            std::optional<bool> process;
            if (fresh) process = false;
            else if (ws == "dead" || ws == "identity_mismatch") process = true;
            observe("worker:" + suffix, "worker_process_anomaly", process, ws, "matching_live_process", identity);
            for (const auto &direction : {std::string("receive"), std::string("send")}) {
                const auto key = direction + ":" + suffix;
                uint64_t threshold = direction == "receive" ? config_.receive_threshold : config_.send_threshold;
                const auto existing = states_.find(key);
                if (existing != states_.end() && !existing->second.active.is_null())
                    threshold = existing->second.active.at("threshold").get<uint64_t>();
                J value = nullptr;
                std::optional<bool> bad;
                if (fresh) { value = row.at("metrics").at(direction + "_queue_messages"); bad = value.get<uint64_t>() >= threshold; }
                observe(key, direction + "_queue_backlog", bad, value, threshold, identity);
            }
        }
    }
    for (auto it = states_.begin(); it != states_.end();) {
        if (!seen.count(it->first)) it->second.since.reset();
        // Keep active incidents across missing data; retire idle disappeared identities.
        if (!seen.count(it->first) && it->second.active.is_null()) it = states_.erase(it);
        else ++it;
    }
    return changes;
}
}
