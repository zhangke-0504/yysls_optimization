#pragma once

#include <chrono>
#include <cstdint>
#include <optional>
#include <string_view>

namespace memory {

struct Settings {
    int threshold = 80;
    int intervalMinutes = 60;
    int cooldownMinutes = 30;
};

std::optional<int> parseSetting(std::wstring_view text, int minimum, int maximum);
std::optional<std::int64_t> availableDelta(std::optional<std::uint64_t> before,
                                            std::optional<std::uint64_t> after);

enum class Trigger { none, manual, threshold, timer };

class Schedule {
public:
    using Clock = std::chrono::steady_clock;
    using Time = Clock::time_point;

    void start(Settings settings, Time now);
    void stop() { enabled_ = false; }
    bool enabled() const { return enabled_; }
    void cleanupStarted(Time now) { lastStart_ = now; }
    Trigger poll(Time now, std::optional<unsigned> load, bool busy);

private:
    Settings settings_;
    bool enabled_ = false;
    Time nextDue_{};
    std::optional<Time> lastStart_;
};

} // namespace memory
