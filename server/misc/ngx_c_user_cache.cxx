
#include "ngx_c_user_cache.h"

#include "ngx_c_conf.h"
#include "ngx_func.h"

#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>

static const char *kNullUserSentinel = "NULL_USER";

const char *CUserCacheService::NullUserSentinel()
{
    return kNullUserSentinel;
}

int CUserCacheService::GetUserInfoCacheTtlSec()
{
    return CConfig::GetInstance()->GetIntDefault("UserInfoCacheTtlSec", 3600);
}

int CUserCacheService::GetNullUserCacheTtlSec()
{
    return CConfig::GetInstance()->GetIntDefault("NullUserCacheTtlSec", 60);
}

static std::string BuildCacheKey(int64_t userId)
{
    char buf[64] = {0};
    snprintf(buf, sizeof(buf) - 1, "user:info:%lld", (long long)userId);
    return std::string(buf);
}

static bool BuildUserJson(const UserInfoDto &info, std::string &jsonOut)
{
    char buf[256] = {0};
    int n = snprintf(buf, sizeof(buf) - 1,
                     "{\"id\":%lld,\"username\":\"%s\"}",
                     (long long)info.id,
                     info.username);
    if (n <= 0 || n >= static_cast<int>(sizeof(buf))) {
        return false;
    }
    jsonOut.assign(buf);
    return true;
}

static bool ParseUserJson(const std::string &json, UserInfoDto &out)
{
    long long id = 0;
    char username[56] = {0};
    int matched = sscanf(json.c_str(),
                         "{\"id\":%lld,\"username\":\"%55[^\"]\"}",
                         &id,
                         username);
    if (matched < 2) {
        return false;
    }
    out.id = id;
    strncpy(out.username, username, sizeof(out.username) - 1);
    out.username[sizeof(out.username) - 1] = '\0';
    return true;
}

bool CUserCacheService::TryGetFromCache(sw::redis::Redis *redis, int64_t userId,
                                        UserInfoDto &out, bool *isNullCached)
{
    if (isNullCached) {
        *isNullCached = false;
    }
    if (redis == nullptr || userId <= 0) {
        return false;
    }

    try {
        const std::string key = BuildCacheKey(userId);
        auto val = redis->get(key);
        if (!val) {
            return false;
        }

        if (*val == kNullUserSentinel) {
            if (isNullCached) {
                *isNullCached = true;
            }
            return false;
        }

        if (!ParseUserJson(*val, out)) {
            ngx_log_stderr(0, "TryGetFromCache: JSON 解析失败 key=%s", key.c_str());
            return false;
        }
        return true;
    } catch (const sw::redis::Error &e) {
        ngx_log_stderr(0, "TryGetFromCache Redis 异常(降级查库): %s", e.what());
        return false;
    }
}

void CUserCacheService::SetUserCache(sw::redis::Redis *redis, int64_t userId,
                                     const UserInfoDto &info, int ttlSec)
{
    if (redis == nullptr || userId <= 0) {
        return;
    }
    if (ttlSec <= 0) {
        ttlSec = GetUserInfoCacheTtlSec();
    }

    std::string json;
    if (!BuildUserJson(info, json)) {
        return;
    }

    try {
        const std::string key = BuildCacheKey(userId);
        redis->set(key, json, std::chrono::seconds(ttlSec));
    } catch (const sw::redis::Error &e) {
        ngx_log_stderr(0, "SetUserCache Redis 异常: %s", e.what());
    }
}

void CUserCacheService::SetNullUserCache(sw::redis::Redis *redis, int64_t userId,
                                         int ttlSec)
{
    if (redis == nullptr || userId <= 0) {
        return;
    }
    if (ttlSec <= 0) {
        ttlSec = GetNullUserCacheTtlSec();
    }

    try {
        const std::string key = BuildCacheKey(userId);
        redis->set(key, kNullUserSentinel, std::chrono::seconds(ttlSec));
    } catch (const sw::redis::Error &e) {
        ngx_log_stderr(0, "SetNullUserCache Redis 异常: %s", e.what());
    }
}
