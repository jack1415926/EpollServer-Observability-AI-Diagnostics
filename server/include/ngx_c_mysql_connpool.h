#ifndef __NGX_C_MYSQL_CONNPOOL_H__
#define __NGX_C_MYSQL_CONNPOOL_H__

#include <mysql/mysql.h>

#include <condition_variable>
#include <memory>
#include <mutex>
#include <queue>
#include <string>

#define NGX_DB_OK              0
#define NGX_DB_ERR_DUPLICATE   1
#define NGX_DB_ERR_FAILED      2
#define NGX_DB_ERR_POOL        3

class CMysqlConnPool
{
private:
    CMysqlConnPool();
    ~CMysqlConnPool();

    CMysqlConnPool(const CMysqlConnPool &) = delete;
    CMysqlConnPool &operator=(const CMysqlConnPool &) = delete;

public:
    static CMysqlConnPool *GetInstance();

    bool Init(const char *host, unsigned int port,
              const char *user, const char *password,
              const char *dbname, int poolSize);

    std::shared_ptr<MYSQL> GetConnection();
    void Destroy();

    bool IsInited() const { return m_inited; }

private:
    MYSQL *CreateConnection();
    void ReturnConnection(MYSQL *conn);
    bool PingConnection(MYSQL *conn);

private:
    static CMysqlConnPool *m_instance;

    std::string m_host;
    std::string m_user;
    std::string m_password;
    std::string m_dbname;
    unsigned int m_port;
    int m_poolSize;

    std::queue<MYSQL *> m_idleQueue;
    std::mutex m_mutex;
    std::condition_variable m_cond;

    bool m_inited;
    bool m_shuttingDown;
    bool m_mysqlLibraryInited;
};

#endif
