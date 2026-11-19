"""Process-scoped message-loop tests for the Win32 desktop targets."""

import argparse
import ctypes as ct
from ctypes import wintypes as w
from contextlib import contextmanager
from pathlib import Path
import subprocess
import time


user32 = ct.WinDLL('user32', use_last_error=True)
POINTER_RESULT = ct.c_size_t


def api(name, result, *args):
    function = getattr(user32, name)
    function.argtypes = args
    function.restype = result
    return function


find_window = api('FindWindowW', w.HWND, w.LPCWSTR, w.LPCWSTR)
get_pid = api('GetWindowThreadProcessId', w.DWORD, w.HWND, ct.POINTER(w.DWORD))
get_item = api('GetDlgItem', w.HWND, w.HWND, ct.c_int)
visible = api('IsWindowVisible', w.BOOL, w.HWND)
enabled = api('IsWindowEnabled', w.BOOL, w.HWND)
iconic = api('IsIconic', w.BOOL, w.HWND)
window_rect = api('GetWindowRect', w.BOOL, w.HWND, ct.POINTER(w.RECT))
post = api('PostMessageW', w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
send_timeout = api('SendMessageTimeoutW', w.LPARAM, w.HWND, w.UINT, w.WPARAM,
                   w.LPARAM, w.UINT, w.UINT, ct.POINTER(POINTER_RESULT))

WM_CLOSE, WM_COMMAND, WM_TIMER = 0x10, 0x111, 0x113
WM_GETTEXT, WM_SETTEXT, WM_SYSCOMMAND = 0xD, 0xC, 0x112
TRAY_NOTIFICATION, SC_MINIMIZE, SC_RESTORE = 0x8001, 0xF020, 0xF120


def require(condition, description):
    if not condition:
        raise AssertionError(description)


def send(window, message, wparam=0, lparam=0):
    result = POINTER_RESULT()
    if not send_timeout(window, message, wparam, lparam, 2, 1500, ct.byref(result)):
        raise RuntimeError(f'message {message:x} failed: {ct.get_last_error()}')
    return result.value


def wait_for(description, probe, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.035)
    raise AssertionError(description)


def owned_window(process, class_name):
    hwnd = find_window(class_name, None)
    pid = w.DWORD()
    if hwnd:
        get_pid(hwnd, ct.byref(pid))
    return hwnd if pid.value == process.pid else None


@contextmanager
def launch(path):
    require(not find_window('MemoryOptimizer.Main', None), 'Close any existing optimizer window first')
    with subprocess.Popen([str(path.resolve())], cwd=path.resolve().parent) as process:
        try:
            window = wait_for('main window did not open', lambda: owned_window(process, 'MemoryOptimizer.Main'))
            wait_for('main window was not shown', lambda: visible(window))
            yield process, window
        finally:
            if process.poll() is None:
                window = owned_window(process, 'MemoryOptimizer.Main')
                if window:
                    post(window, WM_CLOSE, 0, 0)
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(3)


def item(window, control):
    return get_item(window, control) or 0


def text(window, control):
    buffer = ct.create_unicode_buffer(512)
    send(item(window, control), WM_GETTEXT, len(buffer), ct.addressof(buffer))
    return buffer.value


def set_text(window, control, value):
    buffer = ct.create_unicode_buffer(value)
    send(item(window, control), WM_SETTEXT, 0, ct.addressof(buffer))


def command(window, control):
    send(window, WM_COMMAND, control, item(window, control))


def rectangle(window):
    r = w.RECT()
    require(window_rect(window, ct.byref(r)), 'could not read window position')
    return r.left, r.top, r.right, r.bottom


def position(x, y):
    return (x & 0xffff) | ((y & 0xffff) << 16)


def test_release(executable):
    with launch(executable) as (process, window):
        require([text(window, i) for i in (110, 111, 112)] == ['80', '60', '30'], 'incorrect defaults')
        require('已停止' in text(window, 118), 'automation should start disabled')
        require('%' in text(window, 119) and all('GiB' in text(window, i) for i in (120, 121, 130)),
                'all live memory counters must have values')
        set_text(window, 121, 'timer probe')
        wait_for('one-second counter refresh did not update the window', lambda: 'GiB' in text(window, 121), 3)

        set_text(window, 110, '80abc')
        command(window, 113)
        require('阈值' in text(window, 129) and '已停止' in text(window, 118), 'invalid threshold was accepted')
        set_text(window, 110, '100')
        command(window, 113)
        require('运行中' in text(window, 118) and not any(enabled(item(window, i)) for i in (110, 111, 112)),
                'automation did not start or settings stayed editable')

        send(window, WM_SYSCOMMAND, SC_MINIMIZE)
        wait_for('tray minimization did not hide the main window', lambda: not visible(window))
        send(window, TRAY_NOTIFICATION, 1, 0x202)  # The Explorer left-button notification format.
        wait_for('tray restore did not show the main window', lambda: visible(window) and not iconic(window))
        require('运行中' in text(window, 118), 'tray restore changed scheduling state')
        command(window, 113)
        require('已停止' in text(window, 118) and all(enabled(item(window, i)) for i in (110, 111, 112)),
                'stopping optimization did not unlock inputs')

        command(window, 115)
        floating = wait_for('floating window unavailable', lambda: owned_window(process, 'MemoryOptimizer.Floating'))
        wait_for('floating window not visible', lambda: visible(floating))
        wait_for('floating and main window disagree on the memory sample',
                 lambda: text(floating, 126).endswith(text(window, 119)))
        command(window, 116)
        left = rectangle(floating)
        command(window, 117)
        right = rectangle(floating)
        require(right[0] > left[0], 'floating window docking did not move left and right')
        send(floating, 0x201, 1, position(15, 12))
        send(floating, 0x200, 1, position(-38, 82))
        send(floating, 0x202, 0, position(15, 12))
        require(rectangle(floating)[:2] != right[:2], 'drag did not change floating window position')
        send(floating, WM_CLOSE)
        require(not visible(floating), 'floating window did not close')

        command(window, 114)
        wait_for('manual cleanup did not publish counts or time', lambda: '裁剪成功' in text(window, 123)
                 and '—' not in text(window, 125), 8)
        require('MiB' in text(window, 124) or '不可用' in text(window, 124), 'missing net-memory result')
        require('清理完成' in text(window, 122), 'real process enumeration did not complete')
        print('release:', *(text(window, i) for i in (119, 123, 124)), flush=True)
        send(window, WM_COMMAND, 128)  # The tray menu's Exit command.
        require(process.wait(5) == 0, 'release did not exit from the tray command')


def test_fixture(executable):
    with launch(executable) as (process, window):
        send(window, WM_SYSCOMMAND, SC_MINIMIZE)
        wait_for('failed tray registration did not keep a taskbar window',
                 lambda: visible(window) and iconic(window))
        send(window, WM_SYSCOMMAND, SC_RESTORE)
        set_text(window, 110, '1')
        command(window, 114)
        require('手动' in text(window, 122) and not enabled(item(window, 114)), 'cleanup did not enter busy state')
        command(window, 113)
        send(window, WM_TIMER, 1)
        command(window, 114)
        require('手动' in text(window, 122), 'concurrent request changed the running operation')
        command(window, 115)
        floating = owned_window(process, 'MemoryOptimizer.Floating')
        require(floating and visible(floating), 'main window did not respond during cleanup')
        command(window, 113)
        require('已停止' in text(window, 118) and '手动' in text(window, 122), 'stop interrupted in-flight request')
        wait_for('fixture did not complete', lambda: '清理完成' in text(window, 122))
        require('-64.0 MiB' in text(window, 124) and '裁剪成功 1' in text(window, 123),
                'negative net change affected the cleanup success count')
        require(enabled(item(window, 114)), 'manual button stayed disabled after completion')
        command(window, 114)
        require('正在清理' in text(window, 122), 'second cleanup did not enter busy state')
        started_exit = time.monotonic()
        send(window, WM_CLOSE)
        require(process.wait(1.3) == 0 and time.monotonic() - started_exit < 1.3,
                'exit did not cancel and join the active worker promptly')
        print('fixture: verified busy responses, signed result, cancellation and tray fallback', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release', type=Path)
    parser.add_argument('fixture', type=Path)
    inputs = parser.parse_args()
    test_release(inputs.release)
    test_fixture(inputs.fixture)


if __name__ == '__main__':
    main()
