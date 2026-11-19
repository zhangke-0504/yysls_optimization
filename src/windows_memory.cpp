#include "windows_memory.h"
#include "policy.h"

#include <psapi.h>
#include <tlhelp32.h>

namespace memory {
namespace {

class Handle {
public:
    explicit Handle(HANDLE value) : value_(value) {}
    ~Handle() { if (valid()) { CloseHandle(value_); } }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    HANDLE get() const { return value_; }
    bool valid() const { return value_ && value_ != INVALID_HANDLE_VALUE; }

private:
    HANDLE value_;
};

std::optional<std::uint64_t> readAvailableMemory() {
    const auto info = readPhysicalMemory();
    return info ? std::optional{info->available} : std::nullopt;
}

CleanupResult trimProcesses(const std::atomic_bool& cancel) {
    CleanupResult result;
    if (cancel.load(std::memory_order_relaxed)) {
        result.completion = Completion::cancelled;
        return result;
    }
    Handle snapshot(CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0));
    if (!snapshot.valid()) {
        result.error = GetLastError();
        result.completion = Completion::enumerationError;
        return result;
    }
    PROCESSENTRY32W entry{};
    entry.dwSize = sizeof(entry);
    BOOL hasEntry = Process32FirstW(snapshot.get(), &entry);
    while (hasEntry) {
        if (cancel.load(std::memory_order_relaxed)) {
            result.completion = Completion::cancelled;
            return result;
        }
        result.count(trimWorkingSet(entry.th32ProcessID));
        hasEntry = Process32NextW(snapshot.get(), &entry);
    }
    const DWORD error = GetLastError();
    if (error != ERROR_NO_MORE_FILES) {
        result.completion = Completion::enumerationError;
        result.error = error;
    }
    return result;
}

} // namespace

std::optional<PhysicalMemory> readPhysicalMemory() {
    MEMORYSTATUSEX info{};
    info.dwLength = sizeof(info);
    if (!GlobalMemoryStatusEx(&info) || !info.ullTotalPhys ||
        info.ullAvailPhys > info.ullTotalPhys || info.dwMemoryLoad > 100) {
        return std::nullopt;
    }
    return PhysicalMemory{info.ullTotalPhys, info.ullAvailPhys, info.dwMemoryLoad};
}

std::optional<PerformanceMemory> readPerformanceMemory() {
    PERFORMANCE_INFORMATION info{};
    info.cb = sizeof(info);
    if (!GetPerformanceInfo(&info, sizeof(info)) || !info.PageSize || !info.CommitLimit) {
        return std::nullopt;
    }
    const auto pageBytes = static_cast<std::uint64_t>(info.PageSize);
    return PerformanceMemory{info.CommitTotal * pageBytes, info.CommitLimit * pageBytes,
                             info.SystemCache * pageBytes};
}

void CleanupResult::count(Outcome outcome) {
    switch (outcome) {
    case Outcome::trimmed: ++trimmed; break;
    case Outcome::skipped: ++skipped; break;
    case Outcome::failed: ++failed; break;
    }
}

Outcome trimWorkingSet(DWORD pid) {
    Handle process(OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid));
    if (!process.valid()) {
        return Outcome::skipped;
    }
    return EmptyWorkingSet(process.get()) ? Outcome::trimmed : Outcome::failed;
}

CleanupResult cleanWorkingSets(const std::atomic_bool& cancel) {
    const auto before = readAvailableMemory();
    auto result = trimProcesses(cancel);
    result.netAvailable = availableDelta(before, readAvailableMemory());
    return result;
}

} // namespace memory
