#pragma once
#include "experiments.h"

namespace epoll_monitor {
class DiagnosisProxy {
    std::string token_, address_;
    std::atomic<unsigned> pending_{0};
public:
    DiagnosisProxy();
    void Handle(const drogon::HttpRequestPtr &request, ExperimentStore::Reply reply);
};
}
