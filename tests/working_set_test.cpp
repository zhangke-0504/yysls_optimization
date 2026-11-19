#include "windows_memory.h"

#include <iostream>
#include <string>
#include <psapi.h>

namespace {
constexpr DWORD readyTimeout = 10000;
constexpr SIZE_T allocationBytes = 64ull * 1024 * 1024;

class Handle {
public:
    explicit Handle(HANDLE handle) : handle_(handle) {}
    ~Handle() { if (handle_) CloseHandle(handle_); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    HANDLE get() const { return handle_; }
private:
    HANDLE handle_;
};

int runChild(const wchar_t* readyName, const wchar_t* stopName) {
    Handle ready(OpenEventW(EVENT_MODIFY_STATE, FALSE, readyName));
    Handle stop(OpenEventW(SYNCHRONIZE, FALSE, stopName));
    if (!ready.get() || !stop.get()) return 1;
    auto* bytes = static_cast<volatile unsigned char*>(
        VirtualAlloc(nullptr, allocationBytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE));
    if (!bytes) return 2;
    SYSTEM_INFO system{};
    GetSystemInfo(&system);
    for (SIZE_T offset = 0; offset < allocationBytes; offset += system.dwPageSize) {
        bytes[offset] = 42;
    }
    const bool ok = SetEvent(ready.get()) && WaitForSingleObject(stop.get(), 30000) == WAIT_OBJECT_0
        && bytes[allocationBytes - system.dwPageSize] == 42;
    VirtualFree(const_cast<unsigned char*>(bytes), 0, MEM_RELEASE);
    return ok ? 0 : 3;
}

std::optional<SIZE_T> workingSet(HANDLE process) {
    PROCESS_MEMORY_COUNTERS data{};
    return GetProcessMemoryInfo(process, &data, sizeof(data)) ? std::optional{data.WorkingSetSize} : std::nullopt;
}
} // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc == 4 && std::wstring_view(argv[1]) == L"--child") return runChild(argv[2], argv[3]);
    if (argc != 1) return 4;
    const auto baseName = L"Local\\MemoryOptimizerTest-" + std::to_wstring(GetCurrentProcessId()) +
                          L"-" + std::to_wstring(GetTickCount64());
    const auto readyName = baseName + L"-ready";
    const auto stopName = baseName + L"-stop";
    Handle ready(CreateEventW(nullptr, TRUE, FALSE, readyName.c_str()));
    Handle stop(CreateEventW(nullptr, TRUE, FALSE, stopName.c_str()));
    if (!ready.get() || !stop.get()) return 5;

    wchar_t path[32768]{};
    const auto length = GetModuleFileNameW(nullptr, path, static_cast<DWORD>(std::size(path)));
    if (!length || length >= std::size(path)) return 6;
    std::wstring command = L"\"" + std::wstring(path, length) + L"\" --child " + readyName + L" " + stopName;
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION info{};
    if (!CreateProcessW(path, command.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW,
                        nullptr, nullptr, &startup, &info)) return 7;
    Handle process(info.hProcess);
    Handle thread(info.hThread);

    bool ok = false;
    if (WaitForSingleObject(ready.get(), readyTimeout) == WAIT_OBJECT_0) {
        const auto before = workingSet(process.get());
        DWORD handlesBefore = 0, handlesAfter = 0;
        const bool measuredHandles = GetProcessHandleCount(GetCurrentProcess(), &handlesBefore) != FALSE;
        const auto result = memory::trimWorkingSet(info.dwProcessId);
        const auto after = workingSet(process.get());
        const bool alive = WaitForSingleObject(process.get(), 0) == WAIT_TIMEOUT;
        const auto denied = memory::trimWorkingSet(0);
        const bool handlesStable = measuredHandles &&
            GetProcessHandleCount(GetCurrentProcess(), &handlesAfter) && handlesBefore == handlesAfter;
        std::cout << "working_set_before_bytes=" << before.value_or(0)
                  << " working_set_after_bytes=" << after.value_or(0)
                  << " trim_succeeded=" << (result == memory::Outcome::trimmed)
                  << " target_alive=" << alive << " handles_stable=" << handlesStable << '\n';
        ok = before && after && *after < *before && result == memory::Outcome::trimmed
             && alive && handlesStable && denied == memory::Outcome::skipped;
    } else {
        std::cerr << "Timed out waiting for the child to populate its working set\n";
    }

    SetEvent(stop.get());
    if (WaitForSingleObject(process.get(), 5000) != WAIT_OBJECT_0) {
        TerminateProcess(process.get(), 8);
        WaitForSingleObject(process.get(), 2000);
        return 8;
    }
    DWORD exitCode = 1;
    GetExitCodeProcess(process.get(), &exitCode);
    return ok && exitCode == 0 ? 0 : 9;
}
