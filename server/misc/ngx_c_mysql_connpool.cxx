
#include "ngx_c_mysql_connpool.h"

#include "ngx_func.h"

#include <chrono>
#include <cstdio>
#include <cstring>

CMysqlConnPool *CMysqlConnPool::m_instance = nullptr;

CMysqlConnPool::CMysqlConnPool()
    : m_port(3306)
    , m_poolSize(0)
    , m_inited(false)
    , m_shuttingDown(false)
    , m_mysqlLibraryInited(false)
{
}

CMysqlConnPool::~CMysqlConnPool()
{
    Destroy();
}

CMysqlConnPool *CMysqlConnPool::GetInstance()
{
    if (m_instance == nullptr) {
        m_instance = new CMysqlConnPool();
    }
    return m_instance;
}

bool CMysqlConnPool::Init(const char *host, unsigned int port,
                          const char *user, const char *password,
                          const char *dbname, int poolSize)
{
  std::unique_lock<std::mutex> lock(m_mutex);

    if (m_inited) {
        return true;
    }
    if (poolSize <= 0) {
        ngx_log_stderr(0, "CMysqlConnPool::Init() poolSize 必须 > 0");
        return false;
    }

    m_host = host ? host : "127.0.0.1";
    m_port = port;
    m_user = user ? user : "";
    m_password = password ? password : "";
    m_dbname = dbname ? dbname : "";
    m_poolSize = poolSize;
    m_shuttingDown = false;

    // 每个进程调用一次；Worker 在 fork 之后 Init，避免父子进程共享同一连接
    if (!m_mysqlLibraryInited) {
        if (mysql_library_init(0, nullptr, nullptr) != 0) {
            ngx_log_stderr(0, "CMysqlConnPool::Init() mysql_library_init 失败");
            return false;
        }
        m_mysqlLibraryInited = true;
    }

    for (int i = 0; i < m_poolSize; ++i) {
        MYSQL *conn = CreateConnection();
        if (conn == nullptr) {
            ngx_log_stderr(0, "CMysqlConnPool::Init() 创建连接 %d/%d 失败", i + 1, m_poolSize);
            while (!m_idleQueue.empty()) {
                MYSQL *c = m_idleQueue.front();
                m_idleQueue.pop();
                mysql_close(c);
            }
            return false;
        }
        m_idleQueue.push(conn);
    }

    m_inited = true;
    ngx_log_stderr(0, "CMysqlConnPool::Init() 成功，池大小=%d，库=%s", m_poolSize, m_dbname.c_str());
    return true;
}

MYSQL *CMysqlConnPool::CreateConnection()
{
    MYSQL *conn = mysql_init(nullptr);
    if (conn == nullptr) {
        ngx_log_stderr(0, "CMysqlConnPool::CreateConnection() mysql_init 失败");
        return nullptr;
    }

    // 连接超时 5 秒，便于启动时快速失败
    unsigned int timeout_sec = 5;
    mysql_options(conn, MYSQL_OPT_CONNECT_TIMEOUT, &timeout_sec);
    mysql_options(conn, MYSQL_OPT_READ_TIMEOUT, &timeout_sec);
    mysql_options(conn, MYSQL_OPT_WRITE_TIMEOUT, &timeout_sec);

    if (mysql_real_connect(conn,
                           m_host.c_str(),
                           m_user.c_str(),
                           m_password.c_str(),
                           m_dbname.c_str(),
                           m_port,
                           nullptr,
                           0) == nullptr) {
        ngx_log_stderr(0, "CMysqlConnPool::CreateConnection() 连接失败: %s", mysql_error(conn));
        mysql_close(conn);
        return nullptr;
    }

    mysql_set_character_set(conn, "utf8mb4");
    return conn;
}

bool CMysqlConnPool::PingConnection(MYSQL *conn)
{
    if (conn == nullptr) {
        return false;
    }
    return mysql_ping(conn) == 0;
}

std::shared_ptr<MYSQL> CMysqlConnPool::GetConnection()
{
    MYSQL *raw = nullptr;

    {
        // unique_lock 比 lock_guard 灵活：可配合 condition_variable::wait / wait_for
        std::unique_lock<std::mutex> lock(m_mutex);

        if (!m_inited || m_shuttingDown) {
            return std::shared_ptr<MYSQL>();
        }

        // 等待空闲连接；最多等 5 秒，避免业务线程永久阻塞
        const auto timeout = std::chrono::seconds(5);
        const bool got = m_cond.wait_for(lock, timeout, [this]() {
            return m_shuttingDown || !m_idleQueue.empty();
        });

        if (m_shuttingDown || !got || m_idleQueue.empty()) {
            ngx_log_stderr(0, "CMysqlConnPool::GetConnection() 等待连接超时或池已关闭");
            return std::shared_ptr<MYSQL>();
        }

        raw = m_idleQueue.front();
        m_idleQueue.pop();
    }

    // --- RAII 核心：自定义删除器 ---
    // shared_ptr 第二个模板参数是 Deleter。当业务代码中 shared_ptr 析构（离开作用域）
    // 或 reset 时，不会 delete MYSQL*，而是调用我们绑定的 lambda，把连接归还队列。
    CMysqlConnPool *pool = this;
    return std::shared_ptr<MYSQL>(raw, [pool](MYSQL *p) {
        if (p != nullptr) {
            pool->ReturnConnection(p);
        }
    });
}

void CMysqlConnPool::ReturnConnection(MYSQL *conn)
{
    if (conn == nullptr) {
        return;
    }

    std::unique_lock<std::mutex> lock(m_mutex);

    if (m_shuttingDown) {
        mysql_close(conn);
        return;
    }

    // 坏连接：ping 失败则关闭并尝试补一条新连接，避免把废连接还给别人
    if (!PingConnection(conn)) {
        ngx_log_stderr(0, "CMysqlConnPool::ReturnConnection() 检测到坏连接，尝试重建");
        mysql_close(conn);
        conn = CreateConnection();
        if (conn == nullptr) {
            ngx_log_stderr(0, "CMysqlConnPool::ReturnConnection() 重建连接失败");
            return;
        }
    }

    m_idleQueue.push(conn);
    // 唤醒一个在 GetConnection() 里 wait 的线程
    m_cond.notify_one();
}

void CMysqlConnPool::Destroy()
{
    std::unique_lock<std::mutex> lock(m_mutex);

    if (!m_inited && m_idleQueue.empty()) {
        if (m_mysqlLibraryInited) {
            mysql_library_end();
            m_mysqlLibraryInited = false;
        }
        return;
    }

    m_shuttingDown = true;
    m_inited = false;

    // 唤醒所有等待 GetConnection 的线程，避免进程退出时挂死
    m_cond.notify_all();

    while (!m_idleQueue.empty()) {
        MYSQL *conn = m_idleQueue.front();
        m_idleQueue.pop();
        mysql_close(conn);
    }

    lock.unlock();

    if (m_mysqlLibraryInited) {
        mysql_library_end();
        m_mysqlLibraryInited = false;
    }

    m_shuttingDown = false;
}
