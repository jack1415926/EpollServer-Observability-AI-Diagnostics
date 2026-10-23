// Tests the real thread-pool implementation, with only business dispatch/logging replaced.
// g++ -std=c++17 -pthread -Iserver/include testscript/test_threadpool_shutdown.cxx \
//     server/misc/ngx_c_memory.cxx -o /tmp/test_threadpool_shutdown
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <thread>
#include <unistd.h>
#include "ngx_c_threadpool.h"
#include "ngx_c_memory.h"

#define __NGX_GBLDEF_H__
#define __NGX_FUNC_H__
void ngx_log_stderr(int, const char*, ...) {}

struct TestSocket {
    std::atomic<int> processed{0};
    std::mutex mutex;
    std::condition_variable ready;
    bool release = false;
    void threadRecvProcFunc(char*) {
        std::unique_lock<std::mutex> lock(mutex);
        ready.wait(lock, [this] { return release; });
        ++processed;
    }
} g_socket;

#include "../server/misc/ngx_c_threadpool.cxx"

int main()
{
    alarm(10); // deadlocks fail instead of hanging a test run
    CMemory::GetInstance();
    CThreadPool pool;
    if (!pool.Create(4)) return 1;
    for (int i = 0; i < 100; ++i)
        pool.inMsgRecvQueueAndSignal(static_cast<char*>(CMemory::GetInstance()->AllocMemory(1, false)));
    std::thread release([] {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        std::lock_guard<std::mutex> lock(g_socket.mutex);
        g_socket.release = true;
        g_socket.ready.notify_all();
    });
    pool.StopAll(); // must finish all 100 jobs, including queued jobs, before returning
    release.join();
    pool.StopAll(); // repeated stop must be harmless
    return g_socket.processed == 100 && pool.getRecvMsgQueueCount() == 0 ? 0 : 1;
}
