#include "collector.h"
#include <iostream>

int main(int argc, char **argv)
{
    if (argc != 4)
        return 1;
    std::cout << epoll_monitor::Collect(argv[1], std::stoull(argv[2]), argv[3]).dump() << '\n';
}
