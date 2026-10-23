#ifndef EPOLL_METRICS_COLLECTOR_H
#define EPOLL_METRICS_COLLECTOR_H

#include <cstdint>
#include <filesystem>
#include <nlohmann/json.hpp>

namespace epoll_monitor {

// Select one explicit Master instance. procRoot is a test seam; the CLI uses /proc.
// Partial sums never substitute for unavailable full-service totals.
nlohmann::json Collect(const std::filesystem::path &manifest, uint64_t maxAgeMs = 3000,
                       const std::filesystem::path &procRoot = "/proc");

}
#endif
