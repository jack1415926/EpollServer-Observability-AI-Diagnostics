#include "tcp_probe.h"
#include <atomic>
#include <chrono>
#include <iostream>
#include <thread>

int main(int argc, char **argv)
{
    try {
        const auto config = epoll_monitor::ReadTcpProbeConfig();
        nlohmann::json master;
        std::cin >> master;
        if (argc > 1) {
            const bool stale = std::string(argv[1]) == "stale";
            std::atomic<int> calls{0};
            epoll_monitor::TcpProbe probe(config, [&] {
                if (++calls > 1 && stale) std::this_thread::sleep_for(std::chrono::milliseconds(1000));
                return nlohmann::json{{"master", master}};
            });
            while (!calls.load()) std::this_thread::sleep_for(std::chrono::milliseconds(1));
            std::this_thread::sleep_for(std::chrono::milliseconds(stale ? 500 : 50));
            if (!stale) master["master_start_ticks"] = master["master_start_ticks"].get<uint64_t>() + 1;
            std::cout << probe.Read(master) << '\n';
        } else std::cout << epoll_monitor::ProbeTcpOnce(config, master) << '\n';
    } catch (const std::exception &) { return 64; }
}
