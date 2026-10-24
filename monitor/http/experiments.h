#pragma once
#include <drogon/drogon.h>
#include <drogon/orm/DbClient.h>
#include <nlohmann/json.hpp>
#include <atomic>

namespace epoll_monitor {
nlohmann::json ValidateExperiment(const nlohmann::json &report);
nlohmann::json CompareExperiments(const nlohmann::json &left, const nlohmann::json &right);

class ExperimentStore {
    std::atomic<unsigned> pending_{0};
    drogon::orm::DbClientPtr db_;
public:
    using Reply = std::function<void(int, const std::string &, const nlohmann::json &)>;
    ExperimentStore();
    void Handle(const drogon::HttpRequestPtr &request, Reply reply);
};
}
