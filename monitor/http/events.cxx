#include "events.h"
#include <charconv>
#include <cstdlib>
#include <iostream>
#include <time.h>

namespace epoll_monitor {
using J = nlohmann::json;
namespace {
uint64_t BootMs()
{
    timespec now{};
    if (clock_gettime(CLOCK_BOOTTIME, &now)) throw std::runtime_error("clock_unavailable");
    return static_cast<uint64_t>(now.tv_sec) * 1000 + now.tv_nsec / 1000000;
}
int64_t UnixMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}
uint64_t Setting(const char *name, uint64_t fallback, uint64_t maximum)
{
    const char *raw = std::getenv(name);
    if (!raw) return fallback;
    const std::string text(raw);
    uint64_t value = 0;
    auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    if (result.ec != std::errc{} || result.ptr != text.data() + text.size() || !value || value > maximum)
        throw std::invalid_argument("invalid_event_configuration");
    return value;
}
}
EventConfig ReadEventConfig()
{
    return {Setting("EPOLL_MONITOR_EVENT_TRIGGER_MS", 3000, 60000),
            Setting("EPOLL_MONITOR_EVENT_RECOVERY_MS", 3000, 60000),
            Setting("EPOLL_MONITOR_RECEIVE_QUEUE_THRESHOLD", 1024, 2147483647),
            Setting("EPOLL_MONITOR_SEND_QUEUE_THRESHOLD", 1024, 2147483647)};
}

EventService::EventService(EventConfig config, const std::string &manifest, std::function<J()> sample)
    : db_(MonitoringDatabase()), instance_(drogon::utils::getSha256(manifest)),
      rules_(config, instance_, [] { return drogon::utils::getUuid(); }), sample_(std::move(sample))
{
    // shortcut: One detector per manifest; add database coordination before running replicas.
    if (!db_) { store_status_ = detection_status_ = "disabled"; return; }
    // Persistence has its own connection and thread; collection never waits for SQL.
    writer_ = std::thread([this] { Persist(); });
    try { detector_ = std::thread([this] { Detect(); }); }
    catch (...) {
        { std::lock_guard<std::mutex> lock(mutex_); stop_ = true; }
        wake_.notify_all(); writer_.join(); throw;
    }
}
EventService::~EventService()
{
    { std::lock_guard<std::mutex> lock(mutex_); stop_ = true; }
    wake_.notify_all();
    if (detector_.joinable()) detector_.join();
    if (writer_.joinable()) writer_.join();
    if (!pending_.empty()) std::cerr << "Unpersisted monitoring events: " << pending_.size() << '\n';
}
void EventService::Detect()
{
    std::unique_lock<std::mutex> lock(mutex_);
    while (!stop_) {
        // Reserve enough capacity for the maximum number of transitions in one sample.
        try {
            if (ready_ && pending_.size() < MaxPending) {
                const auto value = sample_();
                auto candidate = rules_;
                const auto changes = candidate.Observe(value, BootMs(), UnixMs());
                size_t additional = 0;
                for (const auto &event : changes) if (!pending_.count(event.at("id").get<std::string>())) ++additional;
                if (pending_.size() + additional <= MaxPending) {
                    rules_ = std::move(candidate);
                    for (const auto &event : changes) pending_[event.at("id").get<std::string>()] = event;
                    detection_status_ = "running";
                } else { rules_.ResetWindows(); detection_status_ = "backpressure"; }
            } else { rules_.ResetWindows(); detection_status_ = ready_ ? "backpressure" : "initializing"; }
        } catch (const std::exception &) { rules_.ResetWindows(); detection_status_ = "rule_error_or_capacity"; }
        wake_.wait_for(lock, std::chrono::milliseconds(500), [this] { return stop_; });
    }
}
void EventService::Persist()
{
    std::unique_lock<std::mutex> lock(mutex_);
    while (!stop_) {
        try {
            if (!ready_) {
                lock.unlock();
                const auto rows = db_->execSqlSync(
                    "SELECT report FROM monitor_event WHERE instance_id=? AND status='active' LIMIT 12290", instance_);
                if (rows.size() > 12289) throw std::runtime_error("event_capacity_exceeded");
                std::vector<J> active;
                for (const auto &row : rows) active.push_back(J::parse(row["report"].as<std::string>()));
                lock.lock();
                auto restored = rules_;
                for (const auto &event : active) restored.Restore(event);
                rules_ = std::move(restored);
                ready_ = true;
                store_status_ = "ok";
            }
            if (!pending_.empty()) {
                const auto event = pending_.begin()->second;
                const std::string id = event.at("id"), status = event.at("status");
                const int64_t triggered = event.at("triggered_at_unix_ms");
                const auto report = event.dump();
                lock.unlock();
                db_->execSqlSync("INSERT INTO monitor_event(id,instance_id,status,triggered_at_unix_ms,report) "
                    "VALUES(?,?,?,?,?) ON DUPLICATE KEY UPDATE status=VALUES(status),report=VALUES(report)",
                    id, instance_, status, triggered, report);
                lock.lock();
                // A recovery arriving while SQL was running must still be written.
                if (pending_.at(id) == event) pending_.erase(id);
                store_status_ = "ok";
                continue;
            }
        } catch (const std::exception &) {
            if (!lock.owns_lock()) lock.lock();
            store_status_ = "unavailable";
        }
        wake_.wait_for(lock, std::chrono::milliseconds(500), [this] { return stop_; });
    }
}
void EventService::Handle(const drogon::HttpRequestPtr &request, ExperimentStore::Reply reply)
{
    if (!request->query().empty()) { reply(400, "invalid_event_request", nullptr); return; }
    if (!db_) { reply(503, "event_store_disabled", nullptr); return; }
    if (queries_.fetch_add(1) >= 8) { --queries_; reply(503, "event_store_busy", nullptr); return; }
    J detection;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        detection = {{"store_status", store_status_}, {"ready", ready_}, {"pending_writes", pending_.size()},
                     {"status", detection_status_}, {"capacity", MaxPending},
                     {"backpressure", detection_status_ == "backpressure"}, {"instance", instance_}};
    }
    auto done = [this, reply](int status, const std::string &code, const J &data) { --queries_; reply(status, code, data); };
    db_->execSqlAsync("SELECT report FROM monitor_event ORDER BY triggered_at_unix_ms DESC,id DESC LIMIT 100",
        [done, detection](const drogon::orm::Result &rows) {
            try {
                J events = J::array();
                for (const auto &row : rows) events.push_back(J::parse(row["report"].as<std::string>()));
                done(200, "ok", {{"events", events}, {"limit", 100}, {"detection", detection}});
            } catch (const std::exception &) { done(503, "invalid_stored_event", nullptr); }
        }, [done](const drogon::orm::DrogonDbException &) { done(503, "event_store_unavailable", nullptr); });
}
}
