// Real DAO and logger; only the database calls are replaced.
#include "ngx_global.h"
#include "ngx_c_mysql_dao.h"
#include "ngx_c_mysql_connpool.h"
#include "ngx_func.h"
#include <cassert>
#include <cstdio>
#include <string>

pid_t ngx_pid = 123;
unsigned int dbError = 1062;
extern "C" MYSQL_STMT *__wrap_mysql_stmt_init(MYSQL *) { return reinterpret_cast<MYSQL_STMT *>(1); }
extern "C" int __wrap_mysql_stmt_prepare(MYSQL_STMT *, const char *, unsigned long) { return 0; }
extern "C" bool __wrap_mysql_stmt_bind_param(MYSQL_STMT *, MYSQL_BIND *) { return false; }
extern "C" int __wrap_mysql_stmt_execute(MYSQL_STMT *) { return 1; }
extern "C" unsigned int __wrap_mysql_stmt_errno(MYSQL_STMT *) { return dbError; }
extern "C" const char *__wrap_mysql_stmt_error(MYSQL_STMT *) { return "Duplicate entry '%s%d%'"; }
extern "C" bool __wrap_mysql_stmt_close(MYSQL_STMT *) { return false; }

int main()
{
    FILE *file = tmpfile();
    assert(file);
    ngx_log.fd = fileno(file);
    ngx_log.log_level = 8;
    auto connection = reinterpret_cast<MYSQL *>(1);
    assert(CMysqlDao::RegisterUser(connection, "alice", "test") == NGX_DB_ERR_DUPLICATE);
    dbError = 2006;
    assert(CMysqlDao::RegisterUser(connection, "alice", "test") == NGX_DB_ERR_FAILED);
    ngx_log_stderr(0, "literal: %s", "%s %d %% trailing%");
    ngx_log_stderr(0, "counts: %ui/%ui/%ui", UINTPTR_MAX,
                   static_cast<uintptr_t>(2), static_cast<uintptr_t>(3));
    ngx_log_error_core(5, 0, "publisher failure: %s", "%s%d%");
    rewind(file);
    char buffer[4096]{};
    const auto count = fread(buffer, 1, sizeof(buffer), file);
    const std::string log(buffer, count);
    assert(log.find("失败(1062): Duplicate entry '%s%d%'") != std::string::npos);
    assert(log.find("失败(2006): Duplicate entry '%s%d%'") != std::string::npos);
    assert(log.find("literal: %s %d %% trailing%") != std::string::npos);
    assert(log.find("counts: " + std::to_string(UINTPTR_MAX) + "/2/3") != std::string::npos);
    assert(log.find("publisher failure: %s%d%") != std::string::npos);
    fclose(file);
}
