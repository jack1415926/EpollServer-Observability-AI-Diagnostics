#include "collector.h"
#include <charconv>
#include <iostream>
#include <string>

int main(int argc, char **argv)
{
    if (argc == 2 && std::string(argv[1]) == "--help") {
        std::cout << "Usage: epoll-metrics MASTER_MANIFEST [MAX_AGE_MS=3000]\n"
                     "Exit: 0 complete, 2 partial/stopping, 3 unavailable/instance_changed, 64 usage\n";
        return 0;
    }
    uint64_t age = 3000;
    if (argc < 2 || argc > 3)
        return 64;
    if (argc == 3) {
        const std::string value = argv[2];
        const auto parsed = std::from_chars(value.data(), value.data() + value.size(), age);
        if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size() || age == 0 || age > 60000)
            return 64;
    }
    const auto result = epoll_monitor::Collect(argv[1], age);
    std::cout << result.dump() << '\n';
    const auto status = result.at("status").get<std::string>();
    if (status == "complete")
        return 0;
    return status == "partial" || status == "stopping" ? 2 : 3;
}
