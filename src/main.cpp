#include "policy.h"
#include "application.h"

#include <commctrl.h>
#include <shellapi.h>
#include <windowsx.h>

#include <array>
#include <ctime>
#include <cwchar>
#include <iomanip>
#include <locale>
#include <sstream>
#include <string>
#include <thread>

namespace {
using memory::Schedule;
using memory::Trigger;

constexpr wchar_t mainClass[] = L"MemoryOptimizer.Main";
constexpr wchar_t floatClass[] = L"MemoryOptimizer.Floating";
constexpr wchar_t title[] = L"内存优化器";
constexpr int iconResource = 101;
constexpr UINT refreshTimer = 1;
constexpr UINT trayMessage = WM_APP + 1;
constexpr UINT cleanupMessage = WM_APP + 2;
constexpr UINT hideToTrayMessage = WM_APP + 3;
constexpr UINT trayIconId = 1;
constexpr int clientWidth = 612;
constexpr int clientHeight = 498;

enum Control : int {
    thresholdEdit = 110, intervalEdit, cooldownEdit, autoButton, cleanupButton,
    floatButton, dockLeft, dockRight, autoStatus, physicalValue, commitValue,
    cacheValue, progressLabel, countsLabel, deltaLabel, lastLabel, floatLabel,
    trayRestore, trayExit, settingsError, physicalDetail
};

std::wstring formatNumber(double value, int precision, bool showSign = false) {
    std::wostringstream stream;
    stream.imbue(std::locale::classic());
    if (showSign) {
        stream << std::showpos;
    }
    stream << std::fixed << std::setprecision(precision) << value;
    return stream.str();
}

std::wstring gib(std::uint64_t bytes) {
    return formatNumber(static_cast<double>(bytes) / (1024.0 * 1024 * 1024), 2);
}

std::wstring completedAt() {
    const auto time = std::time(nullptr);
    std::tm local{};
    wchar_t value[32]{};
    if (localtime_s(&local, &time) || !wcsftime(value, std::size(value), L"%Y-%m-%d %H:%M:%S", &local)) {
        return L"时间不可用";
    }
    return value;
}

struct ControlSpec {
    int id;
    const wchar_t* type;
    const wchar_t* text;
    DWORD style;
    int x, y, w, h;
    bool emphasis = false;
};

constexpr DWORD textStyle = SS_LEFT | SS_NOPREFIX;
constexpr DWORD editStyle = WS_TABSTOP | ES_AUTOHSCROLL;
constexpr DWORD buttonStyle = WS_TABSTOP | BS_PUSHBUTTON;
constexpr ControlSpec controls[] = {
    {0, L"STATIC", L"内存概览", textStyle, 24, 19, 220, 26, true},
    {autoStatus, L"STATIC", L"自动优化已停止", SS_RIGHT | SS_NOPREFIX, 390, 23, 198, 24},
    {0, L"STATIC", L"物理内存", textStyle, 38, 73, 140, 21},
    {0, L"STATIC", L"系统提交量 / 上限", textStyle, 230, 73, 170, 21},
    {0, L"STATIC", L"系统缓存", textStyle, 422, 73, 145, 21},
    {physicalValue, L"STATIC", L"不可用", textStyle, 38, 98, 165, 23, true},
    {physicalDetail, L"STATIC", L"", textStyle, 38, 121, 165, 20},
    {commitValue, L"STATIC", L"不可用", textStyle, 230, 102, 165, 23, true},
    {cacheValue, L"STATIC", L"不可用", textStyle, 422, 102, 165, 23, true},
    {0, L"STATIC", L"自动清理设置", textStyle, 24, 158, 240, 24, true},
    {0, L"STATIC", L"物理内存阈值 (%)", textStyle, 24, 191, 170, 22},
    {thresholdEdit, L"EDIT", L"80", editStyle, 24, 215, 150, 28},
    {0, L"STATIC", L"清理间隔 (分钟)", textStyle, 216, 191, 170, 22},
    {intervalEdit, L"EDIT", L"60", editStyle, 216, 215, 150, 28},
    {0, L"STATIC", L"阈值冷却 (分钟)", textStyle, 408, 191, 180, 22},
    {cooldownEdit, L"EDIT", L"30", editStyle, 408, 215, 180, 28},
    {autoButton, L"BUTTON", L"启动优化", buttonStyle, 24, 260, 120, 32},
    {cleanupButton, L"BUTTON", L"手动清理", buttonStyle, 156, 260, 120, 32},
    {settingsError, L"STATIC", L"", textStyle, 292, 265, 298, 23},
    {progressLabel, L"STATIC", L"空闲", textStyle, 38, 319, 535, 23, true},
    {countsLabel, L"STATIC", L"尚无清理结果", textStyle, 38, 348, 535, 21},
    {deltaLabel, L"STATIC", L"可用物理内存净变化  —", textStyle, 38, 373, 535, 21},
    {lastLabel, L"STATIC", L"最近完成  —", textStyle, 38, 398, 535, 22},
    {floatButton, L"BUTTON", L"显示悬浮窗", buttonStyle, 24, 449, 126, 30},
    {dockLeft, L"BUTTON", L"左上角", buttonStyle, 162, 449, 88, 30},
    {dockRight, L"BUTTON", L"右上角", buttonStyle, 262, 449, 88, 30},
};

class Application {
public:
    Application(HINSTANCE instance, AppServices services) : instance_(instance), services_(services) {}
    ~Application() {
        cancel_.store(true, std::memory_order_relaxed);
        if (worker_.joinable()) {
            worker_.join();
        }
        if (main_) {
            DestroyWindow(main_);
        }
        if (font_) DeleteObject(font_);
        if (headingFont_) DeleteObject(headingFont_);
        if (floatingFont_) DeleteObject(floatingFont_);
        if (surface_) DeleteObject(surface_);
        if (panel_) DeleteObject(panel_);
        if (dark_) DeleteObject(dark_);
    }

    bool open(int show);
    HWND mainWindow() const { return main_; }

private:
    static LRESULT CALLBACK mainProc(HWND window, UINT msg, WPARAM w, LPARAM l);
    static LRESULT CALLBACK floatingProc(HWND window, UINT msg, WPARAM w, LPARAM l);
    LRESULT dispatch(UINT msg, WPARAM w, LPARAM l);
    LRESULT dispatchFloating(UINT msg, WPARAM w, LPARAM l);
    bool createControls();
    void layout(UINT dpi);
    void layoutFloating(UINT dpi);
    void updateMetrics();
    void updateButtons();
    void toggleAutomation();
    void startCleanup(Trigger reason);
    void finishCleanup();
    void requestExit();
    void setText(int id, const std::wstring& value) const;
    void addTrayIcon();
    void removeTrayIcon();
    void showTrayMenu(POINT position);
    void restoreMain();
    void showFloating(bool show);
    void placeFloating();
    void updateFloating();
    void paintMain();

    HINSTANCE instance_;
    const AppServices services_;
    HWND main_ = nullptr;
    HWND floating_ = nullptr;
    std::array<HWND, std::size(controls)> controls_{};
    HFONT font_ = nullptr, headingFont_ = nullptr, floatingFont_ = nullptr;
    HBRUSH surface_ = nullptr, panel_ = nullptr, dark_ = nullptr;
    HICON icon_ = nullptr;
    UINT taskbarCreated_ = 0;
    bool rightDock_ = true, dragging_ = false;
    POINT dragOffset_{};
    bool trayAdded_ = false, busy_ = false, closing_ = false;
    std::optional<memory::PhysicalMemory> physical_;
    Schedule schedule_;
    std::thread worker_;
    std::atomic_bool cancel_{false};
    std::atomic_bool ready_{false};
    memory::CleanupResult outcome_;
};

bool Application::open(int show) {
    INITCOMMONCONTROLSEX common{sizeof(common), ICC_STANDARD_CLASSES};
    InitCommonControlsEx(&common);
    icon_ = LoadIconW(instance_, MAKEINTRESOURCEW(iconResource));
    surface_ = CreateSolidBrush(RGB(242, 246, 251));
    panel_ = CreateSolidBrush(RGB(255, 255, 255));
    dark_ = CreateSolidBrush(RGB(25, 48, 76));
    if (!surface_ || !panel_ || !dark_) return false;
    taskbarCreated_ = RegisterWindowMessageW(L"TaskbarCreated");

    WNDCLASSW wc{};
    wc.lpfnWndProc = mainProc;
    wc.hInstance = instance_;
    wc.hIcon = icon_;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = surface_;
    wc.lpszClassName = mainClass;
    if (!RegisterClassW(&wc)) return false;
    wc.lpfnWndProc = floatingProc;
    wc.lpszClassName = floatClass;
    wc.hbrBackground = dark_;
    if (!RegisterClassW(&wc)) return false;

    constexpr DWORD style = WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX;
    RECT rect{0, 0, clientWidth, clientHeight};
    AdjustWindowRectExForDpi(&rect, style, FALSE, 0, 96);
    HWND window = CreateWindowExW(0, mainClass, title, style, CW_USEDEFAULT, CW_USEDEFAULT,
        rect.right - rect.left, rect.bottom - rect.top, nullptr, nullptr, instance_, this);
    if (!window) return false;
    const UINT dpi = GetDpiForWindow(main_);
    rect = {0, 0, MulDiv(clientWidth, dpi, 96), MulDiv(clientHeight, dpi, 96)};
    AdjustWindowRectExForDpi(&rect, style, FALSE, 0, dpi);
    SetWindowPos(main_, nullptr, 0, 0, rect.right - rect.left, rect.bottom - rect.top,
                 SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
    addTrayIcon();
    ShowWindow(main_, show);
    UpdateWindow(main_);
    return true;
}

LRESULT CALLBACK Application::mainProc(HWND window, UINT msg, WPARAM w, LPARAM l) {
    auto* app = reinterpret_cast<Application*>(GetWindowLongPtrW(window, GWLP_USERDATA));
    if (msg == WM_NCCREATE) {
        app = static_cast<Application*>(reinterpret_cast<CREATESTRUCTW*>(l)->lpCreateParams);
        app->main_ = window;
        SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(app));
    }
    if (!app) return DefWindowProcW(window, msg, w, l);
    if (msg == WM_NCDESTROY) {
        SetWindowLongPtrW(window, GWLP_USERDATA, 0);
        app->main_ = nullptr;
        return DefWindowProcW(window, msg, w, l);
    }
    return app->dispatch(msg, w, l);
}

LRESULT CALLBACK Application::floatingProc(HWND window, UINT msg, WPARAM w, LPARAM l) {
    auto* app = reinterpret_cast<Application*>(GetWindowLongPtrW(window, GWLP_USERDATA));
    if (msg == WM_NCCREATE) {
        app = static_cast<Application*>(reinterpret_cast<CREATESTRUCTW*>(l)->lpCreateParams);
        app->floating_ = window;
        SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(app));
    }
    if (!app) return DefWindowProcW(window, msg, w, l);
    if (msg == WM_NCDESTROY) {
        SetWindowLongPtrW(window, GWLP_USERDATA, 0);
        app->floating_ = nullptr;
        return DefWindowProcW(window, msg, w, l);
    }
    return app->dispatchFloating(msg, w, l);
}

bool Application::createControls() {
    for (std::size_t i = 0; i < controls_.size(); ++i) {
        const auto& control = controls[i];
        const bool edit = control.id >= thresholdEdit && control.id <= cooldownEdit;
        controls_[i] = CreateWindowExW(edit ? WS_EX_CLIENTEDGE : 0, control.type, control.text,
            WS_CHILD | WS_VISIBLE | control.style, 0, 0, 0, 0, main_,
            reinterpret_cast<HMENU>(static_cast<INT_PTR>(control.id)), instance_, nullptr);
        if (!controls_[i]) return false;
        if (edit) SendMessageW(controls_[i], EM_SETLIMITTEXT, 32, 0);
    }
    layout(GetDpiForWindow(main_));
    return true;
}

void Application::layout(UINT dpi) {
    HFONT nextFont = CreateFontW(-MulDiv(15, dpi, 96), 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, 0, L"Microsoft YaHei UI");
    HFONT nextHeading = CreateFontW(-MulDiv(18, dpi, 96), 0, 0, 0, FW_SEMIBOLD, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, 0, L"Microsoft YaHei UI");
    for (std::size_t i = 0; i < controls_.size(); ++i) {
        const auto& c = controls[i];
        SetWindowPos(controls_[i], nullptr, MulDiv(c.x, dpi, 96), MulDiv(c.y, dpi, 96),
            MulDiv(c.w, dpi, 96), MulDiv(c.h, dpi, 96), SWP_NOZORDER | SWP_NOACTIVATE);
        const HFONT selected = c.emphasis && nextHeading ? nextHeading : nextFont;
        SendMessageW(controls_[i], WM_SETFONT,
            reinterpret_cast<WPARAM>(selected ? selected : GetStockObject(DEFAULT_GUI_FONT)), TRUE);
    }
    if (font_) DeleteObject(font_);
    if (headingFont_) DeleteObject(headingFont_);
    font_ = nextFont;
    headingFont_ = nextHeading;
    InvalidateRect(main_, nullptr, TRUE);
}

void Application::setText(int id, const std::wstring& value) const {
    SetWindowTextW(GetDlgItem(main_, id), value.c_str());
}

void Application::updateMetrics() {
    physical_ = memory::readPhysicalMemory();
    const auto performance = memory::readPerformanceMemory();
    setText(physicalValue, physical_ ? std::to_wstring(physical_->load) + L"%" : L"不可用");
    setText(physicalDetail, physical_ ? gib(physical_->used()) + L" / " + gib(physical_->total) + L" GiB" : L"");
    setText(commitValue, performance ? gib(performance->committed) + L" / " +
        gib(performance->commitLimit) + L" GiB" : L"不可用");
    setText(cacheValue, performance ? gib(performance->systemCache) + L" GiB" : L"不可用");
    updateFloating();
}

void Application::updateButtons() {
    const bool enabled = schedule_.enabled();
    setText(autoStatus, enabled ? L"自动优化运行中" : L"自动优化已停止");
    setText(autoButton, enabled ? L"停止优化" : L"启动优化");
    for (const int id : {thresholdEdit, intervalEdit, cooldownEdit}) {
        EnableWindow(GetDlgItem(main_, id), !enabled && !closing_);
    }
    EnableWindow(GetDlgItem(main_, autoButton), !closing_);
    EnableWindow(GetDlgItem(main_, cleanupButton), !busy_ && !closing_);
}

void Application::toggleAutomation() {
    if (closing_) return;
    if (schedule_.enabled()) {
        schedule_.stop();
        updateButtons();
        return;
    }
    const auto read = [this](int id, int minimum, int maximum, const wchar_t* label) {
        wchar_t text[33]{};
        HWND edit = GetDlgItem(main_, id);
        const auto size = GetWindowTextLengthW(edit);
        if (size > 0 && size < static_cast<int>(std::size(text))) {
            GetWindowTextW(edit, text, static_cast<int>(std::size(text)));
        }
        auto value = memory::parseSetting(text, minimum, maximum);
        if (!value) {
            setText(settingsError, std::wstring(label) + L"请输入 " + std::to_wstring(minimum) +
                L"–" + std::to_wstring(maximum) + L" 的整数");
            SetFocus(edit);
            SendMessageW(edit, EM_SETSEL, 0, -1);
        }
        return value;
    };
    const auto threshold = read(thresholdEdit, 1, 100, L"阈值");
    if (!threshold) return;
    const auto interval = read(intervalEdit, 1, 1440, L"间隔");
    if (!interval) return;
    const auto cooldown = read(cooldownEdit, 0, 1440, L"冷却");
    if (!cooldown) return;
    setText(settingsError, L"");
    schedule_.start({*threshold, *interval, *cooldown}, Schedule::Clock::now());
    updateButtons();
}

void Application::startCleanup(Trigger reason) {
    if (busy_ || closing_ || reason == Trigger::none) return;
    busy_ = true;
    cancel_.store(false, std::memory_order_relaxed);
    ready_.store(false, std::memory_order_relaxed);
    const auto started = Schedule::Clock::now();
    const HWND window = main_;
    updateButtons();
    setText(progressLabel, reason == Trigger::manual ? L"正在清理 · 手动" :
        reason == Trigger::timer ? L"正在清理 · 定时" : L"正在清理 · 超过阈值");
    try {
        worker_ = std::thread([this, window] {
            outcome_ = services_.clean(cancel_);
            ready_.store(true, std::memory_order_release);
            PostMessageW(window, cleanupMessage, 0, 0);
        });
        schedule_.cleanupStarted(started);
    } catch (const std::system_error&) {
        outcome_ = {};
        outcome_.completion = memory::Completion::launchError;
        ready_.store(true, std::memory_order_release);
        finishCleanup();
    }
}

void Application::finishCleanup() {
    if (!busy_ || !ready_.load(std::memory_order_acquire)) return;
    if (worker_.joinable()) worker_.join();
    // Consume timers that became due during this request before returning to idle.
    schedule_.poll(Schedule::Clock::now(), std::nullopt, true);
    busy_ = false;
    ready_.store(false, std::memory_order_relaxed);
    if (closing_) {
        DestroyWindow(main_);
        return;
    }
    switch (outcome_.completion) {
    case memory::Completion::complete: setText(progressLabel, L"清理完成"); break;
    case memory::Completion::enumerationError:
        setText(progressLabel, L"进程枚举未完成 · Windows 错误 " + std::to_wstring(outcome_.error));
        break;
    case memory::Completion::cancelled: setText(progressLabel, L"清理已取消"); break;
    case memory::Completion::launchError: setText(progressLabel, L"无法启动清理线程"); break;
    }
    setText(countsLabel, L"裁剪成功 " + std::to_wstring(outcome_.trimmed) + L"    跳过 " +
        std::to_wstring(outcome_.skipped) + L"    失败 " + std::to_wstring(outcome_.failed));
    setText(deltaLabel, L"可用物理内存净变化  " + (outcome_.netAvailable
        ? formatNumber(static_cast<double>(*outcome_.netAvailable) / (1024 * 1024), 1, true) + L" MiB"
        : L"不可用"));
    setText(lastLabel, L"最近完成  " + completedAt());
    updateMetrics();
    updateButtons();
}

void Application::requestExit() {
    if (closing_) return;
    closing_ = true;
    schedule_.stop();
    cancel_.store(true, std::memory_order_relaxed);
    if (!busy_) {
        DestroyWindow(main_);
    } else {
        updateButtons();
        setText(progressLabel, L"正在结束后台清理…");
    }
}

void Application::addTrayIcon() {
    NOTIFYICONDATAW info{};
    info.cbSize = sizeof(info);
    info.hWnd = main_;
    info.uID = trayIconId;
    info.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
    info.uCallbackMessage = trayMessage;
    info.hIcon = icon_;
    wcscpy_s(info.szTip, title);
    // Use the default notification format, with the icon ID in wParam.
    trayAdded_ = services_.notifyIcon(NIM_ADD, &info) != FALSE;
}

void Application::removeTrayIcon() {
    if (!trayAdded_) return;
    NOTIFYICONDATAW info{};
    info.cbSize = sizeof(info);
    info.hWnd = main_;
    info.uID = trayIconId;
    services_.notifyIcon(NIM_DELETE, &info);
    trayAdded_ = false;
}

void Application::showTrayMenu(POINT position) {
    HMENU menu = CreatePopupMenu();
    if (!menu) return;
    AppendMenuW(menu, MF_STRING, trayRestore, L"显示主窗口");
    AppendMenuW(menu, MF_STRING, trayExit, L"退出");
    SetForegroundWindow(main_);
    TrackPopupMenu(menu, TPM_RIGHTBUTTON,
        position.x, position.y, 0, main_, nullptr);
    DestroyMenu(menu);
    PostMessageW(main_, WM_NULL, 0, 0);
}

void Application::restoreMain() {
    ShowWindow(main_, SW_RESTORE);
    SetForegroundWindow(main_);
}

void Application::placeFloating() {
    if (!floating_) return;
    MONITORINFO monitor{sizeof(monitor)};
    if (!GetMonitorInfoW(MonitorFromWindow(main_, MONITOR_DEFAULTTONEAREST), &monitor)) return;
    RECT current{};
    GetWindowRect(floating_, &current);
    const int inset = MulDiv(16, GetDpiForWindow(main_), 96);
    const int x = rightDock_ ? monitor.rcWork.right - (current.right - current.left) - inset
                             : monitor.rcWork.left + inset;
    SetWindowPos(floating_, HWND_TOPMOST, x, monitor.rcWork.top + inset, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE);
}

void Application::layoutFloating(UINT dpi) {
    const HWND label = GetDlgItem(floating_, floatLabel);
    if (!label) return;
    HFONT next = CreateFontW(-MulDiv(15, dpi, 96), 0, 0, 0, FW_SEMIBOLD, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, 0, L"Microsoft YaHei UI");
    SendMessageW(label, WM_SETFONT, reinterpret_cast<WPARAM>(next ? next : GetStockObject(DEFAULT_GUI_FONT)), TRUE);
    if (floatingFont_) DeleteObject(floatingFont_);
    floatingFont_ = next;
    MoveWindow(label, MulDiv(8, dpi, 96), MulDiv(9, dpi, 96), MulDiv(154, dpi, 96),
        MulDiv(21, dpi, 96), TRUE);
}

void Application::showFloating(bool show) {
    if (show && !floating_) {
        const UINT dpi = GetDpiForWindow(main_);
        constexpr DWORD extended = WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_TOPMOST;
        if (!CreateWindowExW(extended, floatClass, L"内存悬浮窗", WS_POPUP,
            0, 0, MulDiv(170, dpi, 96), MulDiv(39, dpi, 96), nullptr, nullptr, instance_, this)) return;
        SetLayeredWindowAttributes(floating_, 0, 232, LWA_ALPHA);
        layoutFloating(dpi);
        placeFloating();
    }
    if (!floating_) return;
    if (!show && dragging_) ReleaseCapture();
    ShowWindow(floating_, show ? SW_SHOWNOACTIVATE : SW_HIDE);
    setText(floatButton, show ? L"隐藏悬浮窗" : L"显示悬浮窗");
    updateFloating();
}

void Application::updateFloating() {
    if (!floating_ || !IsWindowVisible(floating_)) return;
    const auto text = physical_ ? L"物理内存  " + std::to_wstring(physical_->load) + L"%" : L"物理内存不可用";
    SetWindowTextW(GetDlgItem(floating_, floatLabel), text.c_str());
}

void Application::paintMain() {
    PAINTSTRUCT paint{};
    HDC dc = BeginPaint(main_, &paint);
    const UINT dpi = GetDpiForWindow(main_);
    const auto box = [dc, dpi](RECT r, HBRUSH brush) {
        r = {MulDiv(r.left, dpi, 96), MulDiv(r.top, dpi, 96),
             MulDiv(r.right, dpi, 96), MulDiv(r.bottom, dpi, 96)};
        FillRect(dc, &r, brush);
    };
    box({24, 58, 204, 145}, panel_);
    box({216, 58, 396, 145}, panel_);
    box({408, 58, 588, 145}, panel_);
    box({24, 309, 588, 433}, panel_);
    EndPaint(main_, &paint);
}

LRESULT Application::dispatch(UINT msg, WPARAM w, LPARAM l) {
    if (taskbarCreated_ && msg == taskbarCreated_) {
        trayAdded_ = false;
        if (!closing_) addTrayIcon();
        if (!trayAdded_ && !IsWindowVisible(main_)) restoreMain();
        return 0;
    }
    switch (msg) {
    case WM_CREATE:
        if (!createControls() || !SetTimer(main_, refreshTimer, 1000, nullptr)) return -1;
        updateMetrics();
        return 0;
    case WM_PAINT:
        paintMain();
        return 0;
    case WM_CTLCOLORSTATIC: {
        HWND child = reinterpret_cast<HWND>(l);
        RECT start{};
        GetWindowRect(child, &start);
        POINT position{start.left, start.top};
        ScreenToClient(main_, &position);
        const int y = MulDiv(position.y, 96, GetDpiForWindow(main_));
        const bool inPanel = (y >= 58 && y < 145) || (y >= 309 && y < 433);
        const auto dc = reinterpret_cast<HDC>(w);
        SetBkColor(dc, inPanel ? RGB(255, 255, 255) : RGB(242, 246, 251));
        SetTextColor(dc, GetDlgCtrlID(child) == settingsError ? RGB(162, 47, 49) : RGB(25, 48, 76));
        return reinterpret_cast<LRESULT>(inPanel ? panel_ : surface_);
    }
    case WM_DPICHANGED: {
        const auto& rect = *reinterpret_cast<const RECT*>(l);
        SetWindowPos(main_, nullptr, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top,
                     SWP_NOZORDER | SWP_NOACTIVATE);
        layout(HIWORD(w));
        return 0;
    }
    case WM_TIMER:
        if (w != refreshTimer) break;
        if (busy_ && ready_.load(std::memory_order_acquire)) {
            finishCleanup();
            return 0;
        }
        if (!closing_) {
            updateMetrics();
            startCleanup(schedule_.poll(Schedule::Clock::now(),
                physical_ ? std::optional{physical_->load} : std::nullopt, busy_));
        }
        return 0;
    case cleanupMessage:
        finishCleanup();
        return 0;
    case WM_COMMAND:
        if (HIWORD(w) != BN_CLICKED || closing_) return 0;
        switch (LOWORD(w)) {
        case autoButton: toggleAutomation(); break;
        case cleanupButton: startCleanup(Trigger::manual); break;
        case floatButton: showFloating(!floating_ || !IsWindowVisible(floating_)); break;
        case dockLeft: rightDock_ = false; placeFloating(); break;
        case dockRight: rightDock_ = true; placeFloating(); break;
        case trayRestore: restoreMain(); break;
        case trayExit: requestExit(); break;
        }
        return 0;
    case WM_SIZE:
        if (w == SIZE_MINIMIZED && trayAdded_) PostMessageW(main_, hideToTrayMessage, 0, 0);
        return 0;
    case hideToTrayMessage:
        if (trayAdded_ && !closing_ && IsIconic(main_)) ShowWindow(main_, SW_HIDE);
        return 0;
    case trayMessage:
        if (w == trayIconId && !closing_) {
            if (l == WM_LBUTTONDBLCLK || l == WM_LBUTTONUP) restoreMain();
            if (l == WM_RBUTTONUP || l == WM_CONTEXTMENU) {
                POINT cursor{};
                GetCursorPos(&cursor);
                showTrayMenu(cursor);
            }
        }
        return 0;
    case WM_CLOSE:
        requestExit();
        return 0;
    case WM_DESTROY:
        KillTimer(main_, refreshTimer);
        removeTrayIcon();
        if (floating_) DestroyWindow(floating_);
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(main_, msg, w, l);
}

LRESULT Application::dispatchFloating(UINT msg, WPARAM w, LPARAM l) {
    switch (msg) {
    case WM_CREATE:
        return CreateWindowExW(0, L"STATIC", L"", WS_CHILD | WS_VISIBLE | SS_CENTER,
            0, 0, 0, 0, floating_, reinterpret_cast<HMENU>(static_cast<INT_PTR>(floatLabel)),
            instance_, nullptr) ? 0 : -1;
    case WM_CTLCOLORSTATIC:
        SetTextColor(reinterpret_cast<HDC>(w), RGB(255, 255, 255));
        SetBkColor(reinterpret_cast<HDC>(w), RGB(25, 48, 76));
        return reinterpret_cast<LRESULT>(dark_);
    case WM_LBUTTONDOWN:
        dragOffset_ = {GET_X_LPARAM(l), GET_Y_LPARAM(l)};
        SetCapture(floating_);
        dragging_ = GetCapture() == floating_;
        return 0;
    case WM_MOUSEMOVE:
        if (dragging_) {
            POINT cursor{GET_X_LPARAM(l), GET_Y_LPARAM(l)};
            if (ClientToScreen(floating_, &cursor)) {
                SetWindowPos(floating_, nullptr, cursor.x - dragOffset_.x, cursor.y - dragOffset_.y,
                    0, 0, SWP_NOZORDER | SWP_NOSIZE | SWP_NOACTIVATE);
            }
        }
        return 0;
    case WM_LBUTTONUP:
        if (dragging_) ReleaseCapture();
        return 0;
    case WM_CAPTURECHANGED:
        dragging_ = false;
        return 0;
    case WM_DPICHANGED: {
        const auto& rect = *reinterpret_cast<const RECT*>(l);
        SetWindowPos(floating_, nullptr, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top,
                     SWP_NOZORDER | SWP_NOACTIVATE);
        layoutFloating(HIWORD(w));
        return 0;
    }
    case WM_RBUTTONUP:
    case WM_CLOSE:
        showFloating(false);
        return 0;
    }
    return DefWindowProcW(floating_, msg, w, l);
}
} // namespace

int runApplication(HINSTANCE instance, int show, AppServices services) {
    Application app(instance, services);
    if (!app.open(show)) {
        MessageBoxW(nullptr, L"无法创建内存优化器窗口。", title, MB_OK | MB_ICONERROR);
        return 1;
    }
    MSG message{};
    BOOL status;
    while ((status = GetMessageW(&message, nullptr, 0, 0)) > 0) {
        if (app.mainWindow() && IsDialogMessageW(app.mainWindow(), &message)) continue;
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    return status == -1 ? 1 : static_cast<int>(message.wParam);
}
