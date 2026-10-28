#ifndef EPOLL_METRICS_COLLECTOR_H
#define EPOLL_METRICS_COLLECTOR_H

#include <cstdint>
#include <filesystem>
#include <map>
#include <string>
#include <nlohmann/json.hpp>

namespace epoll_monitor {

// Owned by one sampling loop. CPU baselines never cross process/instance identities.
class ResourceSampler {
    struct Previous { uint64_t start, cpu, time; };
    std::string instance_;
    std::map<int, Previous> previous_;
public:
    void Enrich(nlohmann::json &sample, const std::filesystem::path &procRoot);
    void Reset() { instance_.clear(); previous_.clear(); }
};

// Select one explicit Master instance. procRoot is a test seam; the CLI uses /proc.
// Partial sums never substitute for unavailable full-service totals.
nlohmann::json Collect(const std::filesystem::path &manifest, uint64_t maxAgeMs = 3000,
                       const std::filesystem::path &procRoot = "/proc", ResourceSampler *resources = nullptr);

}
#endif
