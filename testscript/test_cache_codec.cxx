// Exercise the real internal codec; unused Redis/config paths are removed by --gc-sections.
#include "../server/misc/ngx_c_user_cache.cxx"
#include <cassert>
#include <vector>

int main()
{
    for (const auto &name : std::vector<std::string>{"", "alice\"admin", "a\\b\nc:d", "中文用户名", std::string(55, 'x')}) {
        UserInfoDto input{}, output{};
        input.id = INT64_MAX;
        memcpy(input.username, name.data(), name.size());
        std::string encoded;
        assert(EncodeUserCache(input, encoded));
        assert(DecodeUserCache(encoded, output));
        assert(output.id == input.id && name == output.username);
    }
    UserInfoDto output{};
    for (const auto &invalid : std::vector<std::string>{
             "{\"id\":42,\"username\":\"alice\"admin\"}", "USER_V1:0:x", "USER_V1:-1:x",
             "USER_V1:42junk:x", "USER_V1::x", "USER_V1:9223372036854775808:x",
             "USER_V1:42:" + std::string(56, 'x'), std::string("USER_V1:42:x\0y", 14)})
        assert(!DecodeUserCache(invalid, output));
}
