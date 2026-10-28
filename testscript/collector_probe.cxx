#include "collector.h"
#include "event_rules.h"
#include <iostream>
#include <string>

int main(int argc, char **argv)
{
    if (argc != 4 && argc != 5)
        return 1;
    if (argc == 5 && std::string(argv[4]) == "resources") {
        epoll_monitor::ResourceSampler sampler;
        for (std::string command; std::getline(std::cin, command) && command != "quit";)
            std::cout << epoll_monitor::Collect(argv[1], std::stoull(argv[2]), argv[3], &sampler).dump() << std::endl;
        return 0;
    }
    const auto sample = epoll_monitor::Collect(argv[1], std::stoull(argv[2]), argv[3]);
    if (argc == 4) std::cout << sample.dump() << '\n';
    else {
        unsigned serial = 0;
        epoll_monitor::EventRules rules({}, "test", [&] { return std::to_string(++serial); });
        auto events = nlohmann::json::array();
        for (uint64_t now = 0; now <= 3000; now += 500)
            for (const auto &event : rules.Observe(sample, now, 100000 + now)) events.push_back(event);
        std::cout << events.dump() << '\n';
    }
}
