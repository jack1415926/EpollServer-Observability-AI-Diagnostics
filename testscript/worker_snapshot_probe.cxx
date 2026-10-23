// Standalone Linux probe for the real publisher, without MySQL/Redis dependencies.
#include "ngx_worker_snapshot.h"
#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <iostream>
#include <sstream>

void ngx_log_error_core(int, int, const char *format, ...)
{
    va_list args;
    va_start(args, format);
    vfprintf(stderr, format, args);
    va_end(args);
    fputc('\n', stderr);
}

int main(int argc, char **argv)
{
    if (argc != 2)
        return 1;
    std::atomic<int> connections{3}, receive{5}, send{7};
    CWorkerSnapshot snapshots;
    if (!snapshots.Start(argv[1], 7, [&] {
        return SWorkerMetrics{connections.load(), receive.load(), send.load()};
    }))
        return 2;
    std::string command;
    while (std::getline(std::cin, command) && command != "quit") {
        int a, b, c;
        std::istringstream input(command);
        if (input >> a >> b >> c) {
            connections = a;
            receive = b;
            send = c;
        }
    }
}
