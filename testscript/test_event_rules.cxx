#include "event_rules.h"
#include <cassert>
#include <iostream>
using J = nlohmann::json;
using epoll_monitor::EventRules;
J Sample(std::string status = "fresh", uint64_t receive = 0, uint64_t send = 0)
{
    return {{"status", status == "fresh" ? "complete" : "partial"}, {"reason", "test"},
        {"master", {{"state", "running"}}}, {"workers", J::array({{
            {"pid", 12}, {"process_start_ticks", 34}, {"worker_index", 0}, {"status", status},
            {"metrics", {{"receive_queue_messages", receive}, {"send_queue_messages", send}}}}})}};
}
int main()
{
    unsigned serial = 0;
    auto id = [&] { return std::to_string(++serial); };
    auto tick = [](EventRules &rules, const J &sample, uint64_t now) { return rules.Observe(sample, now, 100000 + now); };
    EventRules rules({}, "instance", id);
    for (uint64_t n = 0; n < 3000; n += 500) assert(tick(rules, Sample("fresh", 1024), n).empty());
    auto changes = tick(rules, Sample("fresh", 1024), 3000);
    assert(changes.size() == 1 && changes[0]["rule"] == "receive_queue_backlog");
    const auto active = changes[0];
    for (uint64_t n = 3500; n < 6000; n += 500) assert(tick(rules, Sample("fresh", 4096), n).empty());
    for (uint64_t n = 6000; n < 8000; n += 500) assert(tick(rules, Sample(), n).empty());
    // Missing queue data interrupts recovery; it never implies zero.
    assert(tick(rules, Sample("stale"), 8000).empty());
    for (uint64_t n = 8500; n < 11500; n += 500) assert(tick(rules, Sample(), n).empty());
    changes = tick(rules, Sample(), 11500);
    assert(changes.size() == 1 && changes[0]["id"] == active["id"] && changes[0]["status"] == "resolved");
    // A fresh recurrence has a different event ID.
    for (uint64_t n = 12000; n < 15000; n += 500) assert(tick(rules, Sample("fresh", 1024), n).empty());
    changes = tick(rules, Sample("fresh", 1024), 15000);
    assert(changes.size() == 1 && changes[0]["id"] != active["id"]);
    EventRules restored({}, "instance", id);
    restored.Restore(active);
    for (uint64_t n = 0; n <= 4000; n += 500) assert(tick(restored, Sample("fresh", 2048), n).empty());
    for (uint64_t n = 4500; n < 7500; n += 500) assert(tick(restored, Sample(), n).empty());
    changes = tick(restored, Sample(), 7500);
    assert(changes.size() == 1 && changes[0]["id"] == active["id"]);
    EventRules worker({}, "instance", id);
    for (uint64_t n = 0; n < 3000; n += 500) assert(tick(worker, Sample("dead"), n).empty());
    changes = tick(worker, Sample("dead"), 3000);
    assert(changes.size() == 2); // Collection + proven process anomaly, no queue event.
    J stopping = Sample("dead"); stopping["status"] = "stopping";
    for (uint64_t n = 3500; n <= 8000; n += 500) assert(tick(worker, stopping, n).empty());
    J gone = Sample(); gone["status"] = "unavailable"; gone["master"] = nullptr; gone["workers"] = J::array();
    for (uint64_t n = 8500; n <= 13000; n += 500) assert(tick(worker, gone, n).empty());
    EventRules gaps({}, "instance", id);
    tick(gaps, Sample("fresh", 0, 1024), 0);
    assert(tick(gaps, Sample("fresh", 0, 1024), 3000).empty());
    for (uint64_t n = 3500; n < 6000; n += 500) assert(tick(gaps, Sample("fresh", 0, 1024), n).empty());
    changes = tick(gaps, Sample("fresh", 0, 1024), 6000);
    assert(changes.size() == 1 && changes[0]["rule"] == "send_queue_backlog");
    // Identity permission failures are unknown, not evidence of an exited process.
    EventRules unknown({}, "other", id);
    unknown.Restore(active);
    for (uint64_t n = 0; n < 3000; n += 500) assert(tick(unknown, Sample("process_unavailable"), n).empty());
    changes = tick(unknown, Sample("process_unavailable"), 3000);
    assert(changes.size() == 1 && changes[0]["rule"] == "collection_unavailable");
    std::cout << "event rule assertions passed\n";
}
