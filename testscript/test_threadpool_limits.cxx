#include <atomic>
#include <cassert>
#include <condition_variable>
#include <mutex>
#include <thread>
#include <unistd.h>
#include "ngx_c_threadpool.h"
#include "ngx_c_memory.h"

#define __NGX_GBLDEF_H__
#define __NGX_FUNC_H__
void ngx_log_stderr(int, const char *, ...) {}
struct TestSocket {
    std::mutex mutex;
    std::condition_variable ready;
    bool entered = false, release = false;
    std::atomic<int> processed{0};
    void threadRecvProcFunc(char *) {
        std::unique_lock<std::mutex> lock(mutex);
        entered = true;
        ready.notify_all();
        ready.wait(lock, [this] { return release; });
        ++processed;
    }
} g_socket;
#include "../server/misc/ngx_c_threadpool.cxx"

int main(int argc, char **)
{
    alarm(15);
    CMemory::GetInstance();
    CThreadPool pool;
    assert(pool.Create(1));
    auto enqueue = [&](size_t bytes) {
        auto buffer = static_cast<char *>(CMemory::GetInstance()->AllocMemory(bytes, false));
        const bool accepted = pool.inMsgRecvQueueAndSignal(buffer, bytes);
        if (!accepted) CMemory::GetInstance()->FreeMemory(buffer);
        return accepted;
    };
    assert(enqueue(1));
    {
        std::unique_lock<std::mutex> lock(g_socket.mutex);
        g_socket.ready.wait(lock, [] { return g_socket.entered; });
    }
    const bool byteLimit = argc > 1;
    const int queued = byteLimit ? 2 : CThreadPool::MaxReceiveQueueMessages;
    const size_t bytes = byteLimit ? CThreadPool::MaxReceiveQueueBytes / 2 : 1;
    for (int i = 0; i < queued; ++i) assert(enqueue(bytes));
    assert(!enqueue(1));
    assert(pool.getRecvMsgQueueCount() == queued);
    {
        std::lock_guard<std::mutex> lock(g_socket.mutex);
        g_socket.release = true;
        g_socket.ready.notify_all();
    }
    while (g_socket.processed != queued + 1) std::this_thread::yield();
    assert(enqueue(CThreadPool::MaxReceiveQueueBytes)); // dequeued bytes restore admission capacity
    pool.StopAll();
    assert(g_socket.processed == queued + 2 && pool.getRecvMsgQueueCount() == 0);
    assert(!enqueue(1)); // no ownership transfer after shutdown
}
