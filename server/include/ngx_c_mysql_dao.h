#ifndef __NGX_C_MYSQL_DAO_H__
#define __NGX_C_MYSQL_DAO_H__

#include <mysql/mysql.h>

#include "ngx_c_user_cache.h"

// 使用连接池借出的 MYSQL* 执行持久化（调用方通过 GetConnection 获取）
class CMysqlDao
{
public:
    // 成功 NGX_DB_OK；重复 NGX_DB_ERR_DUPLICATE；其它 NGX_DB_ERR_FAILED
    static int RegisterUser(MYSQL *conn, const char *username, const char *password);

    // 成功 NGX_DB_OK；校验失败 NGX_DB_ERR_FAILED（用户名或密码错误）
    static int VerifyLogin(MYSQL *conn, const char *username, const char *password);

    // 成功 NGX_DB_OK；无记录 NGX_DB_ERR_FAILED
    static int GetUserById(MYSQL *conn, int64_t userId, UserInfoDto &out);
};

#endif
