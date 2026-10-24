#include "collector.h"
#include "experiments.h"
#include "web_assets.h"
#include <drogon/drogon.h>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <thread>
#include <time.h>
#include <stdexcept>

namespace {
using MonitorJson = nlohmann::json;
using Clock = std::chrono::steady_clock;
constexpr uint64_t MaxAgeMs = 3000;

uint64_t BootMs()
{
    timespec now{};
    if (clock_gettime(CLOCK_BOOTTIME, &now) != 0)
        throw std::runtime_error("clock_unavailable");
    return static_cast<uint64_t>(now.tv_sec) * 1000 + now.tv_nsec / 1000000;
}

int64_t UnixMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

MonitorJson Unavailable(const char *reason)
{
    const MonitorJson empty = {{"connections_current", nullptr}, {"receive_queue_messages", nullptr},
                        {"send_queue_messages", nullptr}};
    return {{"status", "unavailable"}, {"reason", reason}, {"master", nullptr},
            {"workers", MonitorJson::array()}, {"expected_workers", nullptr}, {"valid_workers", 0},
            {"totals", empty}, {"partial_sums", empty}, {"tcp_health", "not_probed"},
            {"max_age_ms", MaxAgeMs}, {"collected_at_unix_ms", nullptr},
            {"unsupported_metrics", {"cpu_memory", "cumulative_network", "business_counters", "mysql", "redis"}}};
}

class Sampler {
    std::mutex mutex_;
    std::condition_variable wake_;
    bool stop_ = false;
    MonitorJson value_ = Unavailable("initializing");
    uint64_t expires_ = 0;
    std::thread thread_;
public:
    explicit Sampler(std::filesystem::path manifest)
        : thread_([this, manifest] {
            for (;;) {
                // Measure from before I/O so a slow collection cannot extend data validity.
                const auto started = BootMs();
                MonitorJson value;
                try { value = epoll_monitor::Collect(manifest, MaxAgeMs); }
                catch (const std::exception &) { value = Unavailable("collection_error"); }
                uint64_t lifetime = 1500;
                for (const auto &row : value["workers"])
                    if (row["status"] == "fresh")
                        lifetime = std::min(lifetime, MaxAgeMs - std::min(MaxAgeMs, row["age_ms"].get<uint64_t>()));
                std::unique_lock<std::mutex> lock(mutex_);
                value_ = std::move(value);
                expires_ = started + lifetime;
                if (wake_.wait_for(lock, std::chrono::milliseconds(500), [this] { return stop_; }))
                    return;
            }
        }) {}
    ~Sampler()
    {
        { std::lock_guard<std::mutex> lock(mutex_); stop_ = true; }
        wake_.notify_one();
        thread_.join();
    }
    MonitorJson Read()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return BootMs() >= expires_ ? Unavailable("collection_timeout") : value_;
    }
};

drogon::HttpResponsePtr Response(int status, const std::string &code, const MonitorJson &data,
                                 const std::string &validity = "unavailable", bool valid = false)
{
    auto response = drogon::HttpResponse::newHttpResponse();
    response->setStatusCode(static_cast<drogon::HttpStatusCode>(status));
    response->setContentTypeCode(drogon::CT_APPLICATION_JSON);
    response->addHeader("Cache-Control", "no-store");
    response->addHeader("X-Content-Type-Options", "nosniff");
    response->setBody(MonitorJson{{"schema_version", 1}, {"timestamp_unix_ms", UnixMs()}, {"code", code},
                           {"validity", validity}, {"data_valid", valid}, {"data", data}}.dump());
    return response;
}

// Fixed-size state; no per-client map or unbounded request queue.
class RateLimit {
    std::mutex mutex_;
    Clock::time_point updated_ = Clock::now();
    double tokens_ = 100;
public:
    bool Allow()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto now = Clock::now();
        tokens_ = std::min(100.0, tokens_ + std::chrono::duration<double>(now - updated_).count() * 100);
        updated_ = now;
        if (tokens_ < 1) return false;
        tokens_ -= 1;
        return true;
    }
};

bool Matches(const std::string &actual, const std::string &expected)
{
    if (actual.size() != expected.size()) return false;
    unsigned difference = 0;
    for (size_t i = 0; i < actual.size(); ++i)
        difference |= static_cast<unsigned char>(actual[i]) ^ static_cast<unsigned char>(expected[i]);
    return difference == 0;
}
}

int main(int argc, char **argv)
{
    if (argc == 2 && std::string(argv[1]) == "--help") {
        std::cout << "Usage: epoll-monitor ABSOLUTE_MASTER_MANIFEST [PORT=8080]\n"
                     "Required environment: EPOLL_MONITOR_TOKEN (32..256 characters: letters, digits, -._~+/=)\n"
                     "Binds 127.0.0.1 only. Dashboard: /; metrics and experiment APIs: /api/v1/.\n"
                     "Optional MySQL: EPOLL_MONITOR_DB_NAME, _HOST, _PORT, _USER, _PASSWORD.\n";
        return 0;
    }
    const char *rawToken = std::getenv("EPOLL_MONITOR_TOKEN");
    const std::string token = rawToken ? rawToken : "";
    unsigned port = 8080;
    if (argc < 2 || argc > 3 || !std::filesystem::path(argv[1]).is_absolute() ||
        token.size() < 32 || token.size() > 256 ||
        token.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~+/=") != std::string::npos) {
        std::cerr << "Invalid arguments or EPOLL_MONITOR_TOKEN; see --help\n";
        return 64;
    }
    if (argc == 3) {
        const std::string text = argv[2];
        const auto parsed = std::from_chars(text.data(), text.data() + text.size(), port);
        if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() || port == 0 || port > 65535)
            return 64;
    }
    std::unique_ptr<epoll_monitor::ExperimentStore> experiments;
    try { experiments = std::make_unique<epoll_monitor::ExperimentStore>(); }
    catch (const std::exception &) { std::cerr << "Invalid monitoring database configuration\n"; return 64; }
    Sampler sampler(argv[1]);
    RateLimit rate;
    auto &app = drogon::app();
    app.addListener("127.0.0.1", static_cast<uint16_t>(port)).setThreadNum(2)
        .setMaxConnectionNum(64).setMaxConnectionNumPerIP(64)
        .setClientMaxBodySize(131072).setClientMaxMemoryBodySize(131072)
        .setIdleConnectionTimeout(5).setKeepaliveRequestsNumber(100)
        .setPipeliningRequestsNumber(4).setFileTypes({}).setImplicitPageEnable(false)
        .disableSession();
    app.registerSyncAdvice([&](const drogon::HttpRequestPtr &request) -> drogon::HttpResponsePtr {
        if (!rate.Allow()) {
            auto response = Response(429, "rate_limited", nullptr);
            response->addHeader("Retry-After", "1");
            return response;
        }
        const std::string path = request->path();
        for (const auto &asset : epoll_monitor::WebAssets) {
            if (path != asset.path) continue;
            if (request->method() != drogon::Get) return Response(405, "method_not_allowed", nullptr);
            if (!request->body().empty() || !request->query().empty()) return Response(400, "unexpected_input", nullptr);
            auto response = drogon::HttpResponse::newHttpResponse();
            response->setBody(std::string(asset.body));
            response->addHeader("Content-Type", std::string(asset.type));
            response->addHeader("Cache-Control", "no-store");
            response->addHeader("X-Content-Type-Options", "nosniff");
            response->addHeader("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'");
            response->addHeader("Referrer-Policy", "no-referrer");
            return response;
        }
        if (!Matches(request->getHeader("authorization"), "Bearer " + token)) {
            auto response = Response(401, "unauthorized", nullptr);
            response->addHeader("WWW-Authenticate", "Bearer");
            return response;
        }
        const bool experiment = path == "/api/v1/experiments" || path.rfind("/api/v1/experiments/", 0) == 0;
        if (request->body().size() > (experiment ? 65536 : 1024)) return Response(413, "body_too_large", nullptr);
        if (experiment) {
            const bool post = path == "/api/v1/experiments/compare" ||
                              (path == "/api/v1/experiments" && request->method() == drogon::Post);
            if (request->method() != (post ? drogon::Post : drogon::Get))
                return Response(405, "method_not_allowed", nullptr);
            if (!post && !request->body().empty()) return Response(400, "unexpected_input", nullptr);
            return nullptr;
        }
        if (path != "/api/v1/health" && path != "/api/v1/overview" &&
            path != "/api/v1/workers" && path != "/api/v1/metrics")
            return Response(404, "not_found", nullptr);
        if (request->method() != drogon::Get)
            return Response(405, "method_not_allowed", nullptr);
        if (!request->body().empty() || !request->query().empty())
            return Response(400, "unexpected_input", nullptr);
        return nullptr;
    });
    app.registerHandlerViaRegex("/api/v1/experiments(?:/[^/]+)?",
        [&](const drogon::HttpRequestPtr &request, std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            experiments->Handle(request, [callback](int status, const std::string &code, const MonitorJson &data) {
                callback(Response(status, code, data, status < 300 ? "stored" : "unavailable", status < 300));
            });
        });
    for (const std::string endpoint : {"health", "overview", "workers", "metrics"}) {
        app.registerHandler("/api/v1/" + endpoint,
            [&, endpoint](const drogon::HttpRequestPtr &, std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
                MonitorJson value = sampler.Read();
                const auto status = value["status"].get<std::string>();
                const bool valid = status == "complete";
                const bool unavailable = status == "unavailable" || status == "instance_changed";
                MonitorJson data;
                if (endpoint == "health") {
                    data = {{"monitor", "ok"}, {"tcp_health", "not_probed"},
                            {"collection_status", status}, {"reason", value["reason"]}};
                } else {
                    data = value;
                    if (endpoint == "overview") data.erase("workers");
                    if (endpoint == "workers") { data.erase("totals"); data.erase("partial_sums"); }
                    if (endpoint == "metrics") { data.erase("master"); data.erase("workers"); }
                }
                callback(Response(endpoint != "health" && unavailable ? 503 : 200,
                                  valid ? "ok" : value["reason"].get<std::string>(), data, status, valid));
            }, {drogon::Get});
    }
    // Parser-level failures can close the connection before this handler runs.
    app.setCustomErrorHandler([](drogon::HttpStatusCode status, const drogon::HttpRequestPtr &) {
        return Response(static_cast<int>(status), "http_error", nullptr);
    });
    app.run();
}
