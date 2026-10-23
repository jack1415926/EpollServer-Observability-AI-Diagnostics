// Real DAO; distinguish absent rows from each query failure without a database.
#include "ngx_c_mysql_dao.h"
#include "ngx_c_mysql_connpool.h"
#include <cassert>
#include <cstring>

void ngx_log_stderr(int, const char *, ...) {}
int prepareError = 0, executeError = 0, fetchResult = MYSQL_NO_DATA;
MYSQL_BIND *bindings = nullptr;
extern "C" MYSQL_STMT *__wrap_mysql_stmt_init(MYSQL *) { return reinterpret_cast<MYSQL_STMT *>(1); }
extern "C" int __wrap_mysql_stmt_prepare(MYSQL_STMT *, const char *, unsigned long) { return prepareError; }
extern "C" bool __wrap_mysql_stmt_close(MYSQL_STMT *) { return false; }
extern "C" bool __wrap_mysql_stmt_bind_param(MYSQL_STMT *, MYSQL_BIND *) { return false; }
extern "C" int __wrap_mysql_stmt_execute(MYSQL_STMT *) { return executeError; }
extern "C" bool __wrap_mysql_stmt_bind_result(MYSQL_STMT *, MYSQL_BIND *b) { bindings = b; return false; }
extern "C" int __wrap_mysql_stmt_fetch(MYSQL_STMT *) {
    if (fetchResult == 0) {
        *static_cast<int64_t *>(bindings[0].buffer) = 42;
        strcpy(static_cast<char *>(bindings[1].buffer), "alice");
    }
    return fetchResult;
}
int main()
{
    UserInfoDto out{};
    auto connection = reinterpret_cast<MYSQL *>(1);
    assert(CMysqlDao::GetUserById(connection, 42, out) == NGX_DB_NOT_FOUND);
    prepareError = 1;
    assert(CMysqlDao::GetUserById(connection, 42, out) == NGX_DB_ERR_FAILED);
    prepareError = 0;
    executeError = 1;
    assert(CMysqlDao::GetUserById(connection, 42, out) == NGX_DB_ERR_FAILED);
    executeError = 0;
    for (int code : {1, MYSQL_DATA_TRUNCATED}) {
        fetchResult = code;
        assert(CMysqlDao::GetUserById(connection, 42, out) == NGX_DB_ERR_FAILED);
    }
    fetchResult = 0;
    assert(CMysqlDao::GetUserById(connection, 42, out) == NGX_DB_OK);
    assert(out.id == 42 && strcmp(out.username, "alice") == 0);
}
