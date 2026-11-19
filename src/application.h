#pragma once

#include "windows_memory.h"
#include <shellapi.h>

struct AppServices {
    decltype(&memory::cleanWorkingSets) clean = memory::cleanWorkingSets;
    decltype(&Shell_NotifyIconW) notifyIcon = Shell_NotifyIconW;
};

int runApplication(HINSTANCE instance, int show, AppServices services = {});
