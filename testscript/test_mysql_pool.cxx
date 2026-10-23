// Real pool, with only MySQL network calls replaced. No database is contacted.
#include "ngx_c_mysql_connpool.h"
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <future>
#include <thread>
#include <unistd.h>

void ngx_log_stderr(int, const char *, ...) {}
std::atomic<bool> failure{false};
std::atomic<int> connects{0};
std::mutex gate;
std::condition_variable ready;
bool blockPing = false, entered = false, releasePing = false;
bool blockConnect = false, connectEntered = false, releaseConnect = false;
extern "C" MYSQL *__wrap_mysql_real_connect(MYSQL *c, const char *, const char *, const char *,
        const char *, unsigned int, const char *, unsigned long) {
    ++connects;
    std::unique_lock<std::mutex> lock(gate);
    if (blockConnect) {
        connectEntered = true;
        ready.notify_all();
        ready.wait(lock, [] { return releaseConnect; });
    }
    return failure ? nullptr : c;
}
extern "C" int __wrap_mysql_set_character_set(MYSQL *, const char *) { return 0; }
extern "C" int __wrap_mysql_ping(MYSQL *) {
    std::unique_lock<std::mutex> lock(gate);
    if (blockPing) {
        entered = true;
        ready.notify_all();
        ready.wait(lock, [] { return releasePing; });
    }
    return failure ? 1 : 0;
}

int main()
{
    using namespace std::chrono_literals;
    alarm(20);
    auto pool = CMysqlConnPool::GetInstance();
    assert(pool->Init("fixture", 3306, "fixture", "", "fixture", 1));
    auto c = pool->GetConnection();
    assert(c);
    failure = true;
    c.reset();
    assert(!pool->GetConnection()); // failed lazy replacement releases its reserved slot
    failure = false;
    c = pool->GetConnection();
    assert(c && connects == 3);
    c.reset();
    pool->Destroy();

    assert(pool->Init("fixture", 3306, "fixture", "", "fixture", 2));
    c = pool->GetConnection();
    {
        std::lock_guard<std::mutex> lock(gate);
        blockPing = true;
    }
    std::thread returning([&] { c.reset(); });
    {
        std::unique_lock<std::mutex> lock(gate);
        ready.wait(lock, [] { return entered; });
    }
    auto checkout = std::async(std::launch::async, [&] { return pool->GetConnection(); });
    const bool prompt = checkout.wait_for(1s) == std::future_status::ready;
    {
        std::lock_guard<std::mutex> lock(gate);
        releasePing = true;
        blockPing = false;
        ready.notify_all();
    }
    returning.join();
    auto other = checkout.get();
    assert(prompt && other); // a slow return must not lock out an idle connection

    auto destroy = std::async(std::launch::async, [&] { pool->Destroy(); });
    while (pool->IsInited()) std::this_thread::yield();
    assert(!pool->GetConnection());
    assert(destroy.wait_for(50ms) == std::future_status::timeout);
    other.reset();
    assert(destroy.wait_for(1s) == std::future_status::ready);
    destroy.get();
    pool->Destroy(); // idempotent

    assert(pool->Init("fixture", 3306, "fixture", "", "fixture", 1));
    c = pool->GetConnection();
    failure = true;
    c.reset();
    failure = false;
    {
        std::lock_guard<std::mutex> lock(gate);
        blockConnect = true;
    }
    auto creating = std::async(std::launch::async, [&] { return pool->GetConnection(); });
    {
        std::unique_lock<std::mutex> lock(gate);
        ready.wait(lock, [] { return connectEntered; });
    }
    destroy = std::async(std::launch::async, [&] { pool->Destroy(); });
    while (pool->IsInited()) std::this_thread::yield();
    assert(destroy.wait_for(50ms) == std::future_status::timeout);
    {
        std::lock_guard<std::mutex> lock(gate);
        releaseConnect = true;
        ready.notify_all();
    }
    assert(!creating.get()); // shutdown rejects/closes the newly created connection
    assert(destroy.wait_for(1s) == std::future_status::ready);
    destroy.get();
}
