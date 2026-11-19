"""Bounded, read-only watcher for the first new exact-path game instance.

Does not stop, suspend or change the game or existing diagnostic sessions.
Only launches this run's diagnostic children, hidden and below normal priority.
"""

import argparse
import ctypes as C
from ctypes import wintypes as W
import datetime as dt
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time


EXPECTED_IMAGE = r"D:\program_files\Games\yysls\yysls_medium\Engine\Binaries\Win64rh\yysls.exe"
DEFAULT_PM = Path(__file__).resolve().parents[1] / "20261116-213050/tools/PresentMon-2.6.0-x64.exe"


class PROCESSENTRY32W(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                ("th32DefaultHeapID", C.c_size_t), ("th32ModuleID", W.DWORD),
                ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
                ("pcPriClassBase", C.c_int32), ("dwFlags", W.DWORD),
                ("szExeFile", W.WCHAR * 260)]


class Processes:
    def __init__(self):
        if sys.platform != "win32" or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("Requires 64-bit Python on Windows")
        assert C.sizeof(PROCESSENTRY32W) == 568
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
        self.k.CreateToolhelp32Snapshot.restype = W.HANDLE
        for name in ["Process32FirstW", "Process32NextW"]:
            fn = getattr(self.k, name)
            fn.argtypes = [W.HANDLE, C.POINTER(PROCESSENTRY32W)]
            fn.restype = W.BOOL
        self.k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.k.OpenProcess.restype = W.HANDLE
        self.k.CloseHandle.argtypes = [W.HANDLE]
        self.k.CloseHandle.restype = W.BOOL
        self.k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        self.k.QueryFullProcessImageNameW.restype = W.BOOL
        self.k.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
        self.k.GetProcessTimes.restype = W.BOOL
        self.k.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
        self.k.GetExitCodeProcess.restype = W.BOOL
        self.k.GetCurrentProcess.argtypes = []
        self.k.GetCurrentProcess.restype = W.HANDLE
        self.k.SetPriorityClass.argtypes = [W.HANDLE, W.DWORD]
        self.k.SetPriorityClass.restype = W.BOOL

    def running(self, handle):
        code = W.DWORD()
        return bool(self.k.GetExitCodeProcess(handle, C.byref(code))) and code.value == 259

    def find(self, expected, excluded, seen):
        snapshot = self.k.CreateToolhelp32Snapshot(0x2, 0)
        if snapshot == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = C.sizeof(entry)
            ok = self.k.Process32FirstW(snapshot, C.byref(entry))
            while ok:
                pid = int(entry.th32ProcessID)
                if entry.szExeFile.lower() == "yysls.exe" and pid not in excluded:
                    handle = self.k.OpenProcess(0x1000, False, pid)
                    keep = False
                    if handle:
                        try:
                            image, length = C.create_unicode_buffer(32768), W.DWORD(32768)
                            times = [W.FILETIME() for _ in range(4)]
                            if (self.k.QueryFullProcessImageNameW(handle, 0, image, C.byref(length))
                                    and os.path.normcase(os.path.abspath(image.value)) == expected
                                    and self.k.GetProcessTimes(handle, *(C.byref(x) for x in times))):
                                creation = int(times[0].dwLowDateTime) | (int(times[0].dwHighDateTime) << 32)
                                if (pid, creation) not in seen and self.running(handle):
                                    keep = True
                                    return pid, creation, handle, image.value
                        finally:
                            if not keep:
                                self.k.CloseHandle(handle)
                ok = self.k.Process32NextW(snapshot, C.byref(entry))
        finally:
            self.k.CloseHandle(snapshot)
        return None


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New watcher directory")
    parser.add_argument("--exclude-pid", type=int, action="append", default=[30320])
    parser.add_argument("--deadline", default="2026-11-18T00:10:00+08:00")
    parser.add_argument("--expected-image", default=EXPECTED_IMAGE)
    parser.add_argument("--presentmon", type=Path, default=DEFAULT_PM)
    parser.add_argument("--max-runtime-seconds", type=float, default=4 * 3600)
    args = parser.parse_args(argv)
    deadline = dt.datetime.fromisoformat(args.deadline)
    if deadline.tzinfo is None or deadline <= now_utc():
        parser.error("Deadline must be future and include timezone")
    if args.max_runtime_seconds <= 0:
        parser.error("Max runtime must be positive")
    scripts = Path(__file__).resolve().parent
    for path in [scripts / "memory_regions.py", scripts / "pressure_counters.py", args.presentmon]:
        if not path.is_file():
            parser.error(f"Required diagnostic executable missing: {path}")
    api = Processes()
    api.k.SetPriorityClass(api.k.GetCurrentProcess(), 0x4000)
    started = time.monotonic()
    monotonic_end = started + min(args.max_runtime_seconds, (deadline - now_utc()).total_seconds())
    expected = os.path.normcase(os.path.abspath(args.expected_image))
    args.out.mkdir(parents=True, exist_ok=False)
    manifest = {"started_utc": now_utc().isoformat(), "watcher_pid": os.getpid(),
                "deadline": deadline.isoformat(), "excluded_pids": args.exclude_pid,
                "expected_image": args.expected_image, "status": "waiting",
                "behavior": "Only first new exact-path instance; no old processes or game modifications."}
    def save():
        (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    def remaining():
        return max(0.0, min(monotonic_end - time.monotonic(), (deadline - now_utc()).total_seconds()))
    seen = set()
    target = None
    while remaining() > 0:
        target = api.find(expected, set(args.exclude_pid), seen)
        if target:
            break
        time.sleep(min(2, remaining()))
    if not target:
        manifest.update(status="deadline_without_new_instance", finished_utc=now_utc().isoformat())
        save()
        return 0
    pid, creation, game_handle, image = target
    seen.add((pid, creation))
    children = []
    session = args.out / f"game-{pid}-{creation}"
    session.mkdir()
    memory_out = session / "memory"
    pm_session = f"YYSLSDiag_relaunch_{pid}_{os.getpid()}"
    manifest.update(status="capturing", game_pid=pid, creation_filetime=creation,
                    game_image=image, session_directory=str(session), presentmon_session=pm_session,
                    detected_utc=now_utc().isoformat(), children=[])
    save()
    def spawn(name, command):
        if remaining() <= 0 or not api.running(game_handle):
            return
        try:
            with (session / f"{name}.log").open("x", encoding="utf-8") as log:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
            children.append((name, child))
            manifest["children"].append({"name": name, "pid": child.pid, "arguments": command})
        except Exception as exc:
            manifest["children"].append({"name": name, "launch_error": str(exc)})
        save()
    try:
        seconds = str(max(1, math.floor(remaining())))
        spawn("memory", [sys.executable, str(scripts / "memory_regions.py"), "--pid", str(pid),
              "--out", str(memory_out), "--duration-seconds", seconds,
              "--interval-seconds", "5", "--map-interval-seconds", "120", "--max-pages", "16384"])
        seconds = str(max(1, math.floor(remaining())))
        spawn("pressure", [sys.executable, str(scripts / "pressure_counters.py"), "--pid", str(pid),
              "--out", str(session / "pressure.jsonl"), "--duration-seconds", seconds,
              "--interval-seconds", "2", "--deadline", deadline.isoformat()])
        seconds = str(max(1, math.floor(remaining())))
        spawn("presentmon", [str(args.presentmon), "--process_id", str(pid),
              "--output_file", str(session / "frames.csv"), "--timed", seconds,
              "--terminate_after_timed", "--terminate_on_proc_exit", "--qpc_time",
              "--no_track_input", "--track_frame_type", "--no_console_stats",
              "--session_name", pm_session])
        reason = "deadline"
        while remaining() > 0:
            if not api.running(game_handle):
                reason = "target_exited"
                break
            if children and all(child.poll() is not None for _, child in children):
                reason = "all_diagnostic_children_exited"
                break
            time.sleep(min(1, remaining()))
        # Only our own memory collector receives its documented graceful stop.
        # PDH and PresentMon have their own target-exit and bounded timers.
        if memory_out.is_dir():
            (memory_out / "stop.request").write_text(reason, encoding="utf-8")
        wait_until = time.monotonic() + 20
        while any(child.poll() is None for _, child in children) and time.monotonic() < wait_until:
            time.sleep(.25)
        manifest.update(status="finished", reason=reason, finished_utc=now_utc().isoformat(),
                        elapsed_seconds=round(time.monotonic() - started, 3),
                        child_exit_codes={name: child.poll() for name, child in children})
        save()
    finally:
        api.k.CloseHandle(game_handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
