#include "policy.h"
#include "windows_memory.h"

#include <iostream>
#include <limits>

using namespace std::chrono_literals;
using namespace memory;

namespace {
int failures = 0;
void check(bool condition, const char* message) {
    if (!condition) {
        std::cerr << message << '\n';
        ++failures;
    }
}
}

int main() {
    const Settings defaults;
    check(defaults.threshold == 80 && defaults.intervalMinutes == 60 &&
          defaults.cooldownMinutes == 30, "default settings");
    check(parseSetting(L"\t 100 ", 1, 100) == 100, "upper bound with whitespace");
    check(parseSetting(L"0", 0, 1440) == 0, "zero cooldown");
    check(parseSetting(L"1440", 1, 1440) == 1440, "maximum interval");
    for (auto invalid : {L"", L"  ", L"0", L"-1", L"1.2", L"80abc", L"1 0",
                         L"101", L"999999999999999999999999"}) {
        check(!parseSetting(invalid, 1, 100), "reject invalid settings");
    }
    constexpr std::uint64_t mib = 1024 * 1024;
    check(availableDelta(128 * mib, 64 * mib) == -64 * static_cast<std::int64_t>(mib), "negative delta");
    check(availableDelta(0, mib) == static_cast<std::int64_t>(mib), "positive delta");
    check(availableDelta(mib, mib) == 0, "zero delta");
    check(!availableDelta(std::nullopt, mib) && !availableDelta(mib, std::nullopt), "missing samples");
    check(!availableDelta(0, (std::numeric_limits<std::uint64_t>::max)()), "delta beyond signed range");

    CleanupResult result;
    result.count(Outcome::trimmed);
    result.count(Outcome::skipped);
    result.count(Outcome::failed);
    check(result.trimmed == 1 && result.skipped == 1 && result.failed == 1, "distinct outcome counts");
    const std::atomic_bool cancelled{true};
    const auto interrupted = cleanWorkingSets(cancelled);
    check(interrupted.completion == Completion::cancelled && interrupted.trimmed == 0, "early cancellation");

    const Schedule::Time base{};
    Schedule schedule;
    check(schedule.poll(base, 100, false) == Trigger::none, "disabled scheduler");
    schedule.start(defaults, base);
    check(schedule.poll(base, 80, false) == Trigger::none, "equal threshold");
    check(schedule.poll(base, 81, false) == Trigger::threshold, "above threshold");
    schedule.cleanupStarted(base + 29min);
    check(schedule.poll(base + 58min, 81, false) == Trigger::none, "cooldown uses cleanup start");
    check(schedule.poll(base + 59min, 81, false) == Trigger::threshold, "exact cooldown boundary");
    schedule.cleanupStarted(base + 59min);
    check(schedule.poll(base + 60min, 81, false) == Trigger::timer, "timer independent of cooldown");
    check(schedule.poll(base + 60min, 81, true) == Trigger::none, "do not start concurrently");
    check(schedule.poll(base + 120min, std::nullopt, true) == Trigger::none, "consume due timer while busy");
    check(schedule.poll(base + 121min, std::nullopt, false) == Trigger::none, "no timer backlog");
    check(schedule.poll(base + 180min, std::nullopt, false) == Trigger::timer, "timer with missing sample");
    check(schedule.poll(base + 240min, 100, false) == Trigger::timer, "both reasons produce a single trigger");
    schedule.stop();
    check(schedule.poll(base + 300min, 100, false) == Trigger::none, "stop cancels future triggers");
    schedule.start(defaults, base + 301min);
    check(schedule.poll(base + 302min, 10, false) == Trigger::none, "restart resets timer");
    return failures ? 1 : 0;
}
