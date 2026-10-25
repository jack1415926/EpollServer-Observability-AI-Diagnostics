#pragma once
#include "event_rules.h"
#include "experiments.h"
#include <condition_variable>
#include <mutex>
#include <thread>

namespace epoll_monitor {
EventConfig ReadEventConfig();
class EventService {
    static constexpr size_t MaxPending = 16384;
    drogon::orm::DbClientPtr db_;
    std::string instance_;
    EventRules rules_;
    std::function<nlohmann::json()> sample_;
    std::mutex mutex_;
    std::condition_variable wake_;
    bool stop_ = false, ready_ = false;
    std::string store_status_ = "initializing";
    std::string detection_status_ = "initializing";
    std::map<std::string, nlohmann::json> pending_;
    std::atomic<unsigned> queries_{0};
    std::thread detector_, writer_;
    void Detect();
    void Persist();
public:
    EventService(EventConfig config, const std::string &manifest, std::function<nlohmann::json()> sample);
    ~EventService();
    void Handle(const drogon::HttpRequestPtr &request, ExperimentStore::Reply reply);
};
}
