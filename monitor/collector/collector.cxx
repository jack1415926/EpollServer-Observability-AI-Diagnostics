#include "collector.h"

#include <cerrno>
#include <chrono>
#include <climits>
#include <fcntl.h>
#include <fstream>
#include <set>
#include <sstream>
#include <stdexcept>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

namespace epoll_monitor {
namespace {
using Json = nlohmann::json;
struct Error : std::runtime_error { using std::runtime_error::runtime_error; };
struct Fd {
    int value;
    explicit Fd(int descriptor) : value(descriptor) {}
    ~Fd() { if (value != -1) close(value); }
    Fd(const Fd &) = delete;
    Fd &operator=(const Fd &) = delete;
};

uint64_t Number(const Json &object, const char *key, uint64_t minimum = 0,
                uint64_t maximum = UINT64_MAX)
{
    if (!object.contains(key) || !object[key].is_number_integer() ||
        (!object[key].is_number_unsigned() && object[key].get<int64_t>() < 0))
        throw Error("invalid_fields");
    const uint64_t value = object[key].get<uint64_t>();
    if (value < minimum || value > maximum)
        throw Error("invalid_fields");
    return value;
}

std::string String(const Json &object, const char *key)
{
    if (!object.contains(key) || !object[key].is_string())
        throw Error("invalid_fields");
    return object[key].get<std::string>();
}

std::string ReadFile(int directory, const std::string &name, size_t maximum)
{
    Fd fd(openat(directory, name.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK));
    if (fd.value == -1)
        throw Error(errno == ENOENT ? "missing" : "unreadable");
    struct stat info;
    if (fstat(fd.value, &info) == -1 || !S_ISREG(info.st_mode) || info.st_uid != geteuid() ||
        (info.st_mode & (S_IWGRP | S_IWOTH)) || info.st_size < 0 ||
        static_cast<uint64_t>(info.st_size) > maximum)
        throw Error("invalid_file");
    std::string data;
    char buffer[4096];
    for (;;) {
        const ssize_t count = read(fd.value, buffer, sizeof(buffer));
        if (count == -1 && errno == EINTR)
            continue;
        if (count < 0)
            throw Error("unreadable");
        if (count == 0)
            return data;
        data.append(buffer, static_cast<size_t>(count));
        if (data.size() > maximum)
            throw Error("invalid_file");
    }
}

Json Parse(const std::string &data)
{
    // Reject ambiguous duplicate keys and bound nesting in addition to file size.
    std::vector<std::set<std::string>> keys;
    auto callback = [&keys](int depth, Json::parse_event_t event, Json &value) {
        if (depth > 32)
            throw Error("invalid_json");
        if (event == Json::parse_event_t::object_start)
            keys.emplace_back();
        else if (event == Json::parse_event_t::key && !keys.back().insert(value.get<std::string>()).second)
            throw Error("invalid_json");
        else if (event == Json::parse_event_t::object_end)
            keys.pop_back();
        return true;
    };
    try {
        auto result = Json::parse(data, callback);
        if (!result.is_object())
            throw Error("invalid_fields");
        if (Number(result, "schema_version") != 1)
            throw Error("unsupported_schema");
        return result;
    } catch (const Json::exception &) {
        throw Error("invalid_json");
    }
}

std::string BootId(const std::filesystem::path &procRoot)
{
    std::ifstream input(procRoot / "sys/kernel/random/boot_id");
    std::string value;
    std::getline(input, value);
    if (value.size() != 36 || value.find_first_not_of("0123456789abcdef-") != std::string::npos)
        throw Error("boot_identity_unavailable");
    return value;
}

struct Process { uint64_t ticks; int parent; char state; };
Process ReadProcess(const std::filesystem::path &procRoot, int pid)
{
    const auto file = procRoot / std::to_string(pid) / "stat";
    std::ifstream input(file);
    if (!input) {
        std::error_code error;
        const bool exists = std::filesystem::exists(file, error);
        throw Error(!error && !exists ? "dead" : "process_unavailable");
    }
    std::string line;
    std::getline(input, line);
    const auto end = line.rfind(')');
    if (end == std::string::npos)
        throw Error("process_unavailable");
    std::istringstream fields(line.substr(end + 1));
    Process result{};
    std::string token;
    for (int field = 3; field <= 22; ++field) {
        if (!(fields >> token))
            throw Error("process_unavailable");
        try {
            if (field == 3) {
                if (token.size() != 1)
                    throw Error("process_unavailable");
                result.state = token[0];
            } else if (field == 4 || field == 22) {
                size_t used = 0;
                if (token.empty() || token[0] == '-')
                    throw Error("process_unavailable");
                const uint64_t value = std::stoull(token, &used);
                if (used != token.size() || (field == 4 && value > INT_MAX))
                    throw Error("process_unavailable");
                if (field == 4) result.parent = static_cast<int>(value);
                else result.ticks = value;
            }
        } catch (const std::invalid_argument &) { throw Error("process_unavailable"); }
        catch (const std::out_of_range &) { throw Error("process_unavailable"); }
    }
    return result;
}

void CheckProcess(const std::filesystem::path &procRoot, int pid, uint64_t ticks, int parent = -1)
{
    const auto process = ReadProcess(procRoot, pid);
    if (process.state == 'Z' || process.state == 'X' || process.state == 'x')
        throw Error("dead");
    if (process.ticks != ticks || (parent != -1 && process.parent != parent))
        throw Error("identity_mismatch");
}

void ValidateMaster(const Json &master)
{
    Number(master, "master_pid", 1, INT_MAX);
    Number(master, "master_start_ticks", 1);
    Number(master, "sequence", 1);
    Number(master, "write_failures_total");
    const auto expected = Number(master, "expected_workers", 0, 4096);
    const auto state = String(master, "state");
    String(master, "boot_id");
    if ((state != "running" && state != "stopping") || !master.contains("workers") ||
        !master["workers"].is_array() || master["workers"].size() > expected)
        throw Error("invalid_fields");
    std::set<uint64_t> indices, pids;
    for (const auto &worker : master["workers"]) {
        if (!worker.is_object() || expected == 0)
            throw Error("invalid_fields");
        const auto index = Number(worker, "worker_index", 0, expected - 1);
        const auto pid = Number(worker, "pid", 1, INT_MAX);
        Number(worker, "process_start_ticks", 1);
        if (!indices.insert(index).second || !pids.insert(pid).second)
            throw Error("duplicate_worker_identity");
    }
}

const char *metrics[] = {"connections_current", "receive_queue_messages", "send_queue_messages"};
uint64_t BootTimeMs()
{
    struct timespec now;
    if (clock_gettime(CLOCK_BOOTTIME, &now) != 0)
        throw Error("clock_unavailable");
    return static_cast<uint64_t>(now.tv_sec) * 1000 + now.tv_nsec / 1000000;
}
Json EmptyMetrics()
{
    Json result = Json::object();
    for (const auto *key : metrics)
        result[key] = nullptr;
    return result;
}
}

Json Collect(const std::filesystem::path &manifest, uint64_t maxAgeMs,
             const std::filesystem::path &procRoot)
{
    Json result = {{"schema_version", 1}, {"status", "unavailable"}, {"reason", "initializing"},
                   {"master", nullptr}, {"workers", Json::array()}, {"expected_workers", nullptr},
                   {"valid_workers", 0}, {"totals", EmptyMetrics()}, {"partial_sums", EmptyMetrics()},
                   {"max_age_ms", maxAgeMs}, {"tcp_health", "not_probed"},
                   {"collected_at_unix_ms", std::chrono::duration_cast<std::chrono::milliseconds>(
                       std::chrono::system_clock::now().time_since_epoch()).count()},
                   {"unsupported_metrics", {"cpu_memory", "cumulative_network", "business_counters", "mysql", "redis"}}};
    try {
        if (maxAgeMs == 0 || maxAgeMs > 60000)
            throw Error("invalid_max_age");
        const auto directory = manifest.has_parent_path() ? manifest.parent_path() : std::filesystem::path(".");
        Fd fd(open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
        struct stat info;
        if (fd.value == -1 || fstat(fd.value, &info) == -1 || info.st_uid != geteuid() ||
            (info.st_mode & (S_IWGRP | S_IWOTH)))
            throw Error("directory_unavailable");
        const std::string original = ReadFile(fd.value, manifest.filename().string(), 1024 * 1024);
        const Json master = Parse(original);
        ValidateMaster(master);
        const std::string boot = BootId(procRoot);
        const int masterPid = master["master_pid"].get<int>();
        const uint64_t masterTicks = master["master_start_ticks"].get<uint64_t>();
        const std::string name = "master-" + boot + "-" + std::to_string(masterPid) +
                                 "-" + std::to_string(masterTicks) + ".json";
        if (String(master, "boot_id") != boot || manifest.filename() != name)
            throw Error("master_identity_mismatch");
        CheckProcess(procRoot, masterPid, masterTicks);
        result["master"] = master;
        result["expected_workers"] = master["expected_workers"];
        uint64_t valid = 0;
        Json sums = {{metrics[0], 0}, {metrics[1], 0}, {metrics[2], 0}};
        for (const auto &worker : master["workers"]) {
            Json row = worker;
            row["status"] = "unavailable";
            row["metrics"] = EmptyMetrics();
            row["age_ms"] = nullptr;
            try {
                const int pid = worker["pid"].get<int>();
                const auto ticks = worker["process_start_ticks"].get<uint64_t>();
                CheckProcess(procRoot, pid, ticks, masterPid);
                const std::string workerName = "worker-" + boot + "-" + std::to_string(pid) +
                                               "-" + std::to_string(ticks) + ".json";
                const Json snapshot = Parse(ReadFile(fd.value, workerName, 16384));
                if (String(snapshot, "boot_id") != boot || Number(snapshot, "master_pid", 1, INT_MAX) != static_cast<uint64_t>(masterPid) ||
                    Number(snapshot, "master_start_ticks", 1) != masterTicks ||
                    Number(snapshot, "pid", 1, INT_MAX) != static_cast<uint64_t>(pid) ||
                    Number(snapshot, "process_start_ticks", 1) != ticks ||
                    Number(snapshot, "worker_index", 0, 4095) != worker["worker_index"].get<uint64_t>())
                    throw Error("identity_mismatch");
                Number(snapshot, "sequence", 1);
                Number(snapshot, "write_failures_total");
                Number(snapshot, "sampled_at_unix_ms");
                const auto sampled = Number(snapshot, "sampled_at_boottime_ms");
                Json values = Json::object();
                for (const auto *key : metrics)
                    values[key] = Number(snapshot, key, 0, INT_MAX);
                if (!snapshot.contains("collection_ok") || !snapshot["collection_ok"].is_boolean())
                    throw Error("invalid_fields");
                const uint64_t nowMs = BootTimeMs();
                if (sampled > nowMs)
                    throw Error("future_sample");
                row["age_ms"] = nowMs - sampled;
                if (nowMs - sampled > maxAgeMs)
                    throw Error("stale");
                if (!snapshot["collection_ok"].get<bool>())
                    throw Error("collection_failed");
                CheckProcess(procRoot, pid, ticks, masterPid);
                row["status"] = "fresh";
                row["metrics"] = values;
                row["sampled_at_boottime_ms"] = sampled;
                row["sequence"] = snapshot["sequence"];
                row["write_failures_total"] = snapshot["write_failures_total"];
                for (const auto *key : metrics)
                    sums[key] = sums[key].get<uint64_t>() + values[key].get<uint64_t>();
                ++valid;
            } catch (const Error &error) {
                row["status"] = error.what();
            }
            result["workers"].push_back(row);
        }
        // Never report a mixture of memberships across a lifecycle transition.
        try {
            CheckProcess(procRoot, masterPid, masterTicks);
            if (ReadFile(fd.value, manifest.filename().string(), 1024 * 1024) != original)
                throw Error("changed");
            const uint64_t finished = BootTimeMs();
            result["collected_at_boottime_ms"] = finished;
            for (auto &row : result["workers"])
                if (row["status"] == "fresh") {
                    CheckProcess(procRoot, row["pid"].get<int>(), row["process_start_ticks"].get<uint64_t>(), masterPid);
                    const auto sampled = row["sampled_at_boottime_ms"].get<uint64_t>();
                    if (sampled > finished || finished - sampled > maxAgeMs)
                        throw Error("changed");
                    row["age_ms"] = finished - sampled;
                }
        } catch (const Error &) {
            result["workers"] = Json::array();
            result["status"] = "instance_changed";
            result["reason"] = "identity_membership_or_freshness_changed_during_read";
            return result;
        }
        result["valid_workers"] = valid;
        if (valid > 0)
            result["partial_sums"] = sums;
        if (master["state"] == "stopping") {
            result["status"] = "stopping";
            result["reason"] = "master_stopping";
        } else if (valid == 0) {
            result["reason"] = "no_fresh_workers";
        } else if (valid != master["expected_workers"].get<uint64_t>()) {
            result["status"] = "partial";
            result["reason"] = "incomplete_worker_data";
        } else {
            result["status"] = "complete";
            result["reason"] = "all_expected_workers_fresh";
            result["totals"] = sums;
        }
    } catch (const Error &error) {
        result["reason"] = error.what();
    } catch (const Json::exception &) {
        result["reason"] = "invalid_fields";
    } catch (const std::exception &) {
        result["reason"] = "collection_error";
    }
    return result;
}
}
