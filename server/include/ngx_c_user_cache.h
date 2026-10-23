#ifndef __NGX_C_USER_CACHE_H__
#define __NGX_C_USER_CACHE_H__

#include <sw/redis++/redis++.h>

#include <cstdint>

struct UserInfoDto
{
    int64_t id;
    char    username[56];
};

class CUserCacheService
{
public:
    // Cache-Aside 读缓存
    // 返回 true：命中正常用户，out 已填充
    // 返回 false 且 *isNullCached==true：命中空值哨兵（防穿透）
    // 返回 false 且 *isNullCached==false：未命中，应查 MySQL
    static bool TryGetFromCache(sw::redis::Redis *redis, int64_t userId,
                                UserInfoDto &out, bool *isNullCached);

    static void SetUserCache(sw::redis::Redis *redis, int64_t userId,
                             const UserInfoDto &info, int ttlSec);

    static void SetNullUserCache(sw::redis::Redis *redis, int64_t userId,
                                 int ttlSec);

    static int GetUserInfoCacheTtlSec();
    static int GetNullUserCacheTtlSec();

    static const char *NullUserSentinel();
};

#endif
