#include "application.h"

namespace {

// Keep a deterministic in-flight window for the GUI tests. This test build
// runs the real UI without trimming unrelated processes or using the tray.
memory::CleanupResult fixtureCleanup(const std::atomic_bool& cancelled) {
    memory::CleanupResult result;
    for (int tick = 0; tick < 150; ++tick) {
        if (cancelled.load(std::memory_order_relaxed)) {
            result.completion = memory::Completion::cancelled;
            return result;
        }
        Sleep(10);
    }
    result.trimmed = 1;
    result.netAvailable = -64 * 1024 * 1024;
    return result;
}

BOOL WINAPI refuseTray(DWORD, PNOTIFYICONDATAW) {
    return FALSE;
}

} // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int show) {
    return runApplication(instance, show, {fixtureCleanup, refuseTray});
}
