#include "experiments.h"
#include <cmath>
#include <cstdlib>
#include <set>
#include <stdexcept>

namespace epoll_monitor {
namespace {
using J = nlohmann::json;
void Require(bool condition) { if (!condition) throw std::invalid_argument("invalid_experiment"); }
void Keys(const J &object, const std::set<std::string> &keys)
{
    Require(object.is_object());
    for (const auto &item : object.items()) Require(keys.count(item.key()) != 0);
}
void Text(const J &v, size_t maximum = 256)
{
    Require(v.is_string() && !v.get_ref<const std::string &>().empty() && v.get_ref<const std::string &>().size() <= maximum);
    for (unsigned char c : v.get_ref<const std::string &>()) Require(c >= 32 && c != 127);
}
double Number(const J &v, bool nullable = false)
{
    if (nullable && v.is_null()) return 0;
    Require(v.is_number() && std::isfinite(v.get<double>()) && v.get<double>() >= 0 && v.get<double>() <= 1e15);
    return v.get<double>();
}
uint64_t Count(const J &v)
{
    Number(v);
    Require(v.is_number_integer());
    return v.get<uint64_t>();
}
void Hex(const J &v, size_t length)
{
    Text(v, length);
    const auto value = v.get<std::string>();
    Require(value.size() == length && value.find_first_not_of("0123456789abcdef") == std::string::npos);
}
J Parse(const std::string &text)
{
    std::vector<std::set<std::string>> keys;
    return J::parse(text, [&keys](int depth, J::parse_event_t event, J &value) {
        Require(depth <= 16);
        if (event == J::parse_event_t::object_start) keys.emplace_back();
        else if (event == J::parse_event_t::key) Require(keys.back().insert(value.get<std::string>()).second);
        else if (event == J::parse_event_t::object_end) keys.pop_back();
        return true;
    });
}
bool Id(const std::string &id)
{
    return id.size() == 36 && id.find_first_not_of("0123456789abcdef-") == std::string::npos;
}
std::string Env(const char *name, const char *fallback = "")
{
    const auto value = std::getenv(name);
    return value ? value : fallback;
}
std::string Quote(const std::string &value)
{
    Require(value.size() <= 256);
    std::string result = "'";
    for (unsigned char c : value) {
        Require(c >= 32 && c != 127);
        if (c == '\'' || c == '\\') result += '\\';
        result += static_cast<char>(c);
    }
    return result + "'";
}
std::string ConnectionWord(const std::string &value)
{
    Require(!value.empty() && value.size() <= 128 &&
            value.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-") == std::string::npos);
    return value;
}
J Summary(const drogon::orm::Row &row)
{
    const J report = J::parse(row["report"].as<std::string>());
    return {{"id", row["id"].as<std::string>()}, {"created_at", row["created_at"].as<std::string>()},
            {"name", report["name"]}, {"timestamp_utc", report["benchmark"]["timestamp_utc"]},
            {"load", report["benchmark"]["load"]}, {"server", report["benchmark"]["server"]},
            {"result", report["benchmark"]["result"]}};
}
}

nlohmann::json ValidateExperiment(const J &report)
{
    Keys(report, {"name", "benchmark", "resources", "context"});
    Text(report.at("name"), 128);
    const auto &b = report.at("benchmark");
    Keys(b, {"timestamp_utc", "label", "git_commit", "git_dirty", "system", "python", "cpu_count", "memory_total_mib", "target", "server", "load", "result"});
    for (const char *key : {"timestamp_utc", "label", "system", "python"}) Text(b.at(key));
    Text(b.at("git_commit"), 40);
    const auto commit = b["git_commit"].get<std::string>();
    Require(commit.size() >= 7 && commit.find_first_not_of("0123456789abcdef") == std::string::npos);
    Require(b.at("git_dirty").is_boolean());
    Require(Count(b.at("cpu_count")) > 0);
    Number(b.at("memory_total_mib"), true);
    Keys(b.at("target"), {"host", "port"});
    Text(b["target"].at("host"));
    Require(Count(b["target"].at("port")) > 0 && Count(b["target"]["port"]) <= 65535);
    const auto &server = b.at("server");
    Keys(server, {"build_mode", "workers", "worker_threads", "rate_limit_mode"});
    for (const char *key : {"build_mode", "rate_limit_mode"}) Text(server.at(key));
    Require(server["build_mode"] == "debug" || server["build_mode"] == "release" || server["build_mode"] == "unknown");
    Require(server["rate_limit_mode"] == "enabled" || server["rate_limit_mode"] == "disabled" || server["rate_limit_mode"] == "unknown");
    for (const char *key : {"workers", "worker_threads"}) Require(Count(server.at(key)) > 0);
    const auto &load = b.at("load");
    Keys(load, {"concurrency", "requests_per_client", "requested", "processes", "receive_mode", "validation_mode", "socket_mode", "latency_mode", "latency_sample_every", "timeout_sec"});
    for (const char *key : {"concurrency", "requests_per_client", "requested", "processes"}) Require(Count(load.at(key)) > 0);
    Require(Count(load["concurrency"]) <= 2000 && Count(load["processes"]) <= Count(load["concurrency"]));
    Require(Count(load["requested"]) == Count(load["concurrency"]) * Count(load["requests_per_client"]));
    for (const char *key : {"receive_mode", "validation_mode", "socket_mode", "latency_mode"}) Text(load.at(key));
    Require(load["receive_mode"] == "into" || load["receive_mode"] == "exact" || load["receive_mode"] == "single");
    Require(load["validation_mode"] == "strict" || load["validation_mode"] == "length-only");
    Require(load["socket_mode"] == "timeout" || load["socket_mode"] == "kernel-timeout" || load["socket_mode"] == "blocking");
    Require(load["latency_mode"] == "all" || load["latency_mode"] == "sampled" || load["latency_mode"] == "off");
    if (load["latency_mode"] == "sampled") Require(Count(load.at("latency_sample_every")) > 0);
    else Require(load.at("latency_sample_every").is_null());
    if (load.contains("timeout_sec")) Require(Number(load["timeout_sec"]) > 0);
    const auto &result = b.at("result");
    Keys(result, {"duration_sec", "attempted", "successful", "failed", "not_attempted", "success_rate_percent", "qps", "latency_ms", "errors"});
    Require(Number(result.at("duration_sec")) > 0);
    Number(result.at("qps"));
    Require(Number(result.at("success_rate_percent")) <= 100);
    const auto attempted = Count(result.at("attempted")), successful = Count(result.at("successful"));
    Require(successful + Count(result.at("failed")) == attempted);
    Require(attempted + Count(result.at("not_attempted")) == Count(load["requested"]));
    Require(std::abs(Number(result["success_rate_percent"]) - successful * 100.0 / Count(load["requested"])) <= 0.001);
    Keys(result.at("latency_ms"), {"sample_count", "mean", "p50", "p95", "p99", "max"});
    const auto &latency = result["latency_ms"];
    const auto samples = Count(latency.at("sample_count"));
    Require(samples <= successful && (load["latency_mode"] != "off" || samples == 0));
    if (load["latency_mode"] == "all") Require(samples == successful);
    for (const char *key : {"p50", "p95", "p99", "max"}) {
        Number(latency.at(key), true);
        Require(samples == 0 ? latency[key].is_null() : latency[key].is_number());
    }
    if (samples > 0) Require(latency["p50"] <= latency["p95"] && latency["p95"] <= latency["p99"] && latency["p99"] <= latency["max"]);
    if (latency.contains("mean")) {
        Number(latency["mean"], true);
        Require(samples == 0 ? latency["mean"].is_null() : latency["mean"].is_number());
    }
    Require(result.at("errors").is_object() && result["errors"].size() <= 32);
    uint64_t errors = 0;
    for (const auto &item : result["errors"].items()) { Text(item.key(), 80); errors += Count(item.value()); }
    Require(errors == Count(result["failed"]));
    const auto &context = report.at("context");
    Keys(context, {"environment_id", "binary_sha256", "client_sha256", "server_cpus", "client_cpus", "snapshot_enabled", "redis_window_sec", "redis_max_requests", "command", "result_sha256", "resources_sha256"});
    for (const char *key : {"environment_id", "server_cpus", "client_cpus"}) Text(context.at(key));
    for (const char *key : {"binary_sha256", "client_sha256", "result_sha256", "resources_sha256"}) Hex(context.at(key), 64);
    Text(context.at("command"), 4096);
    Require(context.at("snapshot_enabled").is_boolean());
    for (const char *key : {"redis_window_sec", "redis_max_requests"}) Require(Count(context.at(key)) > 0);
    const auto &resources = report.at("resources");
    Keys(resources, {"duration_sec", "interval_sec", "client_exit_code", "process_summary", "system_summary"});
    Number(resources.at("duration_sec")); Number(resources.at("interval_sec"));
    const auto &exitCode = resources.at("client_exit_code");
    Require(exitCode.is_number_integer());
    if (exitCode.is_number_unsigned()) Require(exitCode.get<uint64_t>() <= 255);
    else Require(exitCode.get<int64_t>() >= -255 && exitCode.get<int64_t>() <= 255);
    Require(resources.at("process_summary").is_array() && resources["process_summary"].size() <= 64);
    for (const auto &p : resources["process_summary"]) {
        Keys(p, {"role", "pid", "name", "max_rss_kib", "max_threads", "avg_cpu_percent", "max_cpu_percent"});
        Text(p.at("role"), 32); Text(p.at("name"), 64);
        for (const char *key : {"pid", "max_rss_kib", "max_threads"}) Count(p.at(key));
        for (const char *key : {"avg_cpu_percent", "max_cpu_percent"}) Number(p.at(key), true);
    }
    Require(resources.at("system_summary").is_object() && resources["system_summary"].size() <= 16);
    for (const auto &item : resources["system_summary"].items()) {
        Text(item.key(), 80); Number(item.value(), true);
    }
    Require(report.dump().size() <= 65536);
    return report;
}

nlohmann::json CompareExperiments(const J &left, const J &right)
{
    J differences = J::array();
    for (const char *path : {"/benchmark/system", "/benchmark/python", "/benchmark/cpu_count", "/benchmark/memory_total_mib",
                            "/benchmark/git_commit", "/benchmark/git_dirty", "/benchmark/server", "/benchmark/load", "/benchmark/target/host",
                            "/context/environment_id", "/context/binary_sha256", "/context/client_sha256",
                            "/context/server_cpus", "/context/client_cpus", "/context/snapshot_enabled",
                            "/context/redis_window_sec", "/context/redis_max_requests"})
        if (left.at(J::json_pointer(path)) != right.at(J::json_pointer(path))) differences.push_back(path);
    J metrics = J::object();
    const auto values = [](const J &r) {
        const auto &v = r["benchmark"]["result"];
        const auto requested = r["benchmark"]["load"]["requested"].get<double>();
        return J{{"qps", v["qps"]}, {"p95_ms", v["latency_ms"]["p95"]},
                 {"mean_latency_ms", v["latency_ms"].value("mean", J(nullptr))},
                 {"unsuccessful_percent", (requested - v["successful"].get<double>()) / requested * 100}};
    };
    const auto a = values(left), b = values(right);
    for (const auto &item : a.items()) {
        J delta = nullptr, percent = nullptr;
        if (item.value().is_number() && b[item.key()].is_number()) {
            const double base = item.value().get<double>(), next = b[item.key()].get<double>();
            delta = next - base;
            if (base != 0) percent = (next - base) / base * 100;
        }
        metrics[item.key()] = {{"baseline", item.value()}, {"candidate", b[item.key()]}, {"delta", delta}, {"change_percent", percent}};
    }
    return {{"same_conditions", differences.empty()}, {"condition_differences", differences}, {"metrics", metrics},
            {"interpretation", "observed_difference_only_no_causal_claim"}};
}

ExperimentStore::ExperimentStore()
{
    const auto database = Env("EPOLL_MONITOR_DB_NAME");
    if (database.empty()) return;
    const auto port = Env("EPOLL_MONITOR_DB_PORT", "3306");
    Require(!port.empty() && port.find_first_not_of("0123456789") == std::string::npos && port.size() <= 5);
    Require(std::stoul(port) > 0 && std::stoul(port) <= 65535);
    // Drogon 1.9.11 greedily combines multiple quoted fields; quote only the final password.
    const std::string connection = "host=" + ConnectionWord(Env("EPOLL_MONITOR_DB_HOST", "127.0.0.1")) + " port=" + port +
        " dbname=" + ConnectionWord(database) + " user=" + ConnectionWord(Env("EPOLL_MONITOR_DB_USER", "epoll_monitor")) +
        " client_encoding=utf8mb4 password=" + Quote(Env("EPOLL_MONITOR_DB_PASSWORD"));
    db_ = drogon::orm::DbClient::newMysqlClient(connection, 1);
    db_->setTimeout(2);
}

void ExperimentStore::Handle(const drogon::HttpRequestPtr &request, Reply reply)
{
    const std::string path = request->path();
    J report, comparison;
    const bool importing = path == "/api/v1/experiments" && request->method() == drogon::Post;
    const bool comparing = path == "/api/v1/experiments/compare";
    const bool listing = path == "/api/v1/experiments" && !importing;
    const std::string id = path.substr(std::string("/api/v1/experiments").size());
    try {
        if (importing) report = ValidateExperiment(Parse(std::string(request->body())));
        else if (comparing) {
            comparison = Parse(std::string(request->body()));
            Keys(comparison, {"baseline", "candidate"});
            Require(Id(comparison.at("baseline").get<std::string>()) && Id(comparison.at("candidate").get<std::string>()));
        } else if (!listing) Require(id.size() == 37 && Id(id.substr(1)));
        Require(request->query().empty());
    } catch (const std::exception &) { reply(400, "invalid_experiment_request", nullptr); return; }
    if (!db_) { reply(503, "experiment_store_disabled", nullptr); return; }
    if (pending_.fetch_add(1) >= 8) { --pending_; reply(503, "experiment_store_busy", nullptr); return; }
    auto done = [this, reply](int status, const std::string &code, const J &data) { --pending_; reply(status, code, data); };
    auto failure = [done](const drogon::orm::DrogonDbException &) { done(503, "experiment_store_unavailable", nullptr); };
    if (importing) {
        const std::string uuid = drogon::utils::getUuid();
        db_->execSqlAsync("INSERT INTO monitor_experiment(id, report) VALUES(?, ?)",
            [done, uuid](const drogon::orm::Result &) { done(201, "created", {{"id", uuid}}); }, failure, uuid, report.dump());
    } else if (listing) {
        db_->execSqlAsync("SELECT id, report, created_at FROM monitor_experiment ORDER BY created_at DESC, id DESC LIMIT 100",
            [done](const drogon::orm::Result &rows) {
                try {
                    J items = J::array(); for (const auto &row : rows) items.push_back(Summary(row));
                    done(200, "ok", {{"experiments", items}, {"limit", 100}});
                } catch (const std::exception &) { done(503, "invalid_stored_experiment", nullptr); }
            }, failure);
    } else if (comparing) {
        const std::string baseline = comparison["baseline"], candidate = comparison["candidate"];
        db_->execSqlAsync("SELECT id, report FROM monitor_experiment WHERE id IN (?, ?)",
            [done, baseline, candidate](const drogon::orm::Result &rows) {
                try {
                    J a, b;
                    for (const auto &row : rows) {
                        const std::string key = row["id"].as<std::string>();
                        if (key == baseline) a = J::parse(row["report"].as<std::string>());
                        if (key == candidate) b = J::parse(row["report"].as<std::string>());
                    }
                    if (a.is_null() || b.is_null()) { done(404, "experiment_not_found", nullptr); return; }
                    auto result = CompareExperiments(a, b); result["baseline"] = baseline; result["candidate"] = candidate;
                    done(200, "ok", result);
                } catch (const std::exception &) { done(503, "invalid_stored_experiment", nullptr); }
            }, failure, baseline, candidate);
    } else {
        db_->execSqlAsync("SELECT id, report, created_at FROM monitor_experiment WHERE id = ?",
            [done](const drogon::orm::Result &rows) {
                if (rows.empty()) { done(404, "experiment_not_found", nullptr); return; }
                try {
                    auto value = Summary(rows[0]); value["report"] = J::parse(rows[0]["report"].as<std::string>());
                    done(200, "ok", value);
                } catch (const std::exception &) { done(503, "invalid_stored_experiment", nullptr); }
            }, failure, id.substr(1));
    }
}
}
