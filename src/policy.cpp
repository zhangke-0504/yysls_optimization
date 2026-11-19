#include "policy.h"

#include <limits>

namespace memory {

std::optional<int> parseSetting(std::wstring_view text, int minimum, int maximum) {
    const auto start = text.find_first_not_of(L" \t\r\n");
    if (start == std::wstring_view::npos || maximum < 0) {
        return std::nullopt;
    }
    text = text.substr(start, text.find_last_not_of(L" \t\r\n") - start + 1);
    int value = 0;
    for (wchar_t character : text) {
        if (character < L'0' || character > L'9') {
            return std::nullopt;
        }
        const int digit = character - L'0';
        if (digit > maximum || value > (maximum - digit) / 10) {
            return std::nullopt;
        }
        value = 10 * value + digit;
    }
    return value >= minimum ? std::optional{value} : std::nullopt;
}

std::optional<std::int64_t> availableDelta(std::optional<std::uint64_t> before,
                                            std::optional<std::uint64_t> after) {
    if (!before || !after) {
        return std::nullopt;
    }
    const bool decreasing = *after < *before;
    const auto magnitude = decreasing ? *before - *after : *after - *before;
    constexpr auto limit = (std::numeric_limits<std::int64_t>::max)();
    if (magnitude > static_cast<std::uint64_t>(limit)) {
        return std::nullopt;
    }
    const auto signedMagnitude = static_cast<std::int64_t>(magnitude);
    return decreasing ? -signedMagnitude : signedMagnitude;
}

void Schedule::start(Settings settings, Time now) {
    settings_ = settings;
    enabled_ = true;
    nextDue_ = now + std::chrono::minutes(settings.intervalMinutes);
}

Trigger Schedule::poll(Time now, std::optional<unsigned> load, bool busy) {
    if (!enabled_) {
        return Trigger::none;
    }
    if (now >= nextDue_) {
        nextDue_ = now + std::chrono::minutes(settings_.intervalMinutes);
        return busy ? Trigger::none : Trigger::timer;
    }
    if (busy || !load || *load <= static_cast<unsigned>(settings_.threshold)) {
        return Trigger::none;
    }
    if (lastStart_ && now - *lastStart_ < std::chrono::minutes(settings_.cooldownMinutes)) {
        return Trigger::none;
    }
    return Trigger::threshold;
}

} // namespace memory
