
#include "ngx_c_user_cache.h"

#include "ngx_c_conf.h"
#include "ngx_func.h"

#include <charconv>
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

// A versioned prefix and a single numeric delimiter preserve username bytes without a JSON dependency.
static bool EncodeUserCache(const UserInfoDto &info, std::string &encoded)
{
    const size_t length = strnlen(info.username, sizeof(info.username));
    if (info.id <= 0 || length == sizeof(info.username))
        return false;
    encoded = "USER_V1:" + std::to_string(info.id) + ":" + std::string(info.username, length);
    return true;
}

static bool DecodeUserCache(const std::string &encoded, UserInfoDto &out)
{
    constexpr size_t prefixLength = 8;
    if (encoded.compare(0, prefixLength, "USER_V1:") != 0)
        return false; // Old JSON values are cache misses and are refreshed from MySQL.
    const auto separator = encoded.find(':', prefixLength);
    if (separator == std::string::npos)
        return false;
    int64_t id = 0;
    const auto parsed = std::from_chars(encoded.data() + prefixLength, encoded.data() + separator, id);
    const size_t length = encoded.size() - separator - 1;
    if (parsed.ec != std::errc{} || parsed.ptr != encoded.data() + separator || id <= 0 ||
        length >= sizeof(out.username) || encoded.find('\0', separator + 1) != std::string::npos)
        return false;
    out = {};
    out.id = id;
    memcpy(out.username, encoded.data() + separator + 1, length);
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

        if (!DecodeUserCache(*val, out) || out.id != userId) {
            ngx_log_stderr(0, "TryGetFromCache: 缓存编码无效 key=%s", key.c_str());
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

    std::string encoded;
    if (info.id != userId || !EncodeUserCache(info, encoded)) {
        return;
    }

    try {
        const std::string key = BuildCacheKey(userId);
        redis->set(key, encoded, std::chrono::seconds(ttlSec));
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
