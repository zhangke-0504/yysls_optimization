#pragma once

#include <atomic>
#include <cstdint>
#include <optional>
#include <windows.h>

namespace memory {

struct PhysicalMemory {
    std::uint64_t total;
    std::uint64_t available;
    unsigned load;
    std::uint64_t used() const { return total - available; }
};

struct PerformanceMemory {
    std::uint64_t committed;
    std::uint64_t commitLimit;
    std::uint64_t systemCache;
};

std::optional<PhysicalMemory> readPhysicalMemory();
std::optional<PerformanceMemory> readPerformanceMemory();

enum class Outcome { trimmed, skipped, failed };
enum class Completion { complete, enumerationError, cancelled, launchError };

struct CleanupResult {
    unsigned trimmed = 0;
    unsigned skipped = 0;
    unsigned failed = 0;
    Completion completion = Completion::complete;
    DWORD error = ERROR_SUCCESS;
    std::optional<std::int64_t> netAvailable;

    void count(Outcome outcome);
};

Outcome trimWorkingSet(DWORD pid);
CleanupResult cleanWorkingSets(const std::atomic_bool& cancel);

} // namespace memory
