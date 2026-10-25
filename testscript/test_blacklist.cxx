// Exercise production blacklist methods without network threads or backends.
#include "ngx_c_socket.h"
#include <cassert>
#include <ctime>

CSocekt::CSocekt() {}
CSocekt::~CSocekt() {}
bool CSocekt::Initialize() { return true; }
bool CSocekt::Initialize_subproc() { return true; }
void CSocekt::Shutdown_subproc() {}
void CSocekt::procPingTimeOutChecking(LPSTRUC_MSG_HEADER, time_t) {}

class TestSocket : public CSocekt {
public:
    using CSocekt::MaxBlacklistEntries;
    using CSocekt::PruneIpBlacklist;
    size_t size() const { return m_localBlacklist.size(); }
};

int main()
{
    TestSocket socket;
    const auto now = time(nullptr);
    for (size_t i = 0; i < socket.MaxBlacklistEntries + 100; ++i)
        socket.AddIpToBlacklist(std::to_string(i), 60);
    assert(socket.size() == socket.MaxBlacklistEntries);
    assert(!socket.IsIpBlacklisted(std::to_string(socket.MaxBlacklistEntries)));
    socket.AddIpToBlacklist("0", 1200); // Existing bans can extend at capacity.
    socket.PruneIpBlacklist(now + 600); // No expired source is queried again.
    assert(socket.size() == 1 && socket.IsIpBlacklisted("0"));
    socket.AddIpToBlacklist("new", 60);
    assert(socket.IsIpBlacklisted("new"));
    socket.PruneIpBlacklist(now + 1800);
    assert(socket.size() == 0);
}
