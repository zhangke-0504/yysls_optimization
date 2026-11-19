"""Read-only, bounded Windows x64 PDH sampling. No injection or memory cleaning.

GPU engine instances are retained separately; percentages are never summed.
System page-in counters include mapped-file reads and are not per-game faults.
The first sample's rate values are intentionally null, preserving PDH status.
Single-engine GPU values outside 0..100.5 percent are invalid, with raw values
retained. The 0.5 percent tolerance does not apply to byte or paging counters.
"""

import argparse
import ctypes as C
from ctypes import wintypes as W
import datetime as dt
import json
import math
import os
from pathlib import Path
import sys
import time


PDH_MORE_DATA = 0x800007D2
PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
VALID_STATUSES = {0, 1}


class FMT_UNION(C.Union):
    _fields_ = [("longValue", C.c_int32), ("doubleValue", C.c_double),
                ("largeValue", C.c_int64), ("stringValue", C.c_void_p)]


class FMT_VALUE(C.Structure):
    _fields_ = [("CStatus", C.c_uint32), ("value", FMT_UNION)]


class FMT_ITEM(C.Structure):
    _fields_ = [("szName", W.LPWSTR), ("FmtValue", FMT_VALUE)]


def status(code):
    return f"0x{int(code) & 0xffffffff:08x}"


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def format_instance(counter_name, instance, number, code_status, first_rate):
    finite = math.isfinite(number)
    valid = code_status in VALID_STATUSES and finite and not first_rate
    result = {"instance": instance, "cstatus": status(code_status),
              "value": number if valid else None, "valid": valid}
    if counter_name == "gpu_engines_percent":
        # PDH may return a successful CStatus for an impossible rate after an
        # instance/counter discontinuity. Preserve evidence; never clamp it.
        result["raw_value"] = number if finite else str(number)
        if finite and not 0.0 <= number <= 100.5:
            result.update(value=None, valid=False, invalid_reason="out_of_range",
                          accepted_range_percent=[0.0, 100.5])
        elif not finite:
            result["invalid_reason"] = "non_finite"
        elif code_status not in VALID_STATUSES:
            result["invalid_reason"] = "pdh_counter_status"
        elif first_rate:
            result["invalid_reason"] = "first_rate_sample"
    return result


class PDH:
    def __init__(self):
        if sys.platform != "win32" or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("Requires 64-bit Python on Windows")
        assert C.sizeof(FMT_VALUE) == 16 and FMT_VALUE.value.offset == 8
        assert C.sizeof(FMT_ITEM) == 24 and FMT_ITEM.FmtValue.offset == 8
        self.dll = C.WinDLL("pdh", use_last_error=True)
        self.dll.PdhOpenQueryW.argtypes = [W.LPCWSTR, C.c_size_t, C.POINTER(W.HANDLE)]
        self.dll.PdhOpenQueryW.restype = C.c_uint32
        self.dll.PdhAddEnglishCounterW.argtypes = [W.HANDLE, W.LPCWSTR, C.c_size_t, C.POINTER(W.HANDLE)]
        self.dll.PdhAddEnglishCounterW.restype = C.c_uint32
        self.dll.PdhCollectQueryData.argtypes = [W.HANDLE]
        self.dll.PdhCollectQueryData.restype = C.c_uint32
        self.dll.PdhGetFormattedCounterArrayW.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.c_void_p]
        self.dll.PdhGetFormattedCounterArrayW.restype = C.c_uint32
        self.dll.PdhGetFormattedCounterValue.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.DWORD), C.POINTER(FMT_VALUE)]
        self.dll.PdhGetFormattedCounterValue.restype = C.c_uint32
        self.dll.PdhCloseQuery.argtypes = [W.HANDLE]
        self.dll.PdhCloseQuery.restype = C.c_uint32
        self.query = W.HANDLE()
        code = self.dll.PdhOpenQueryW(None, 0, C.byref(self.query))
        if code:
            raise RuntimeError(f"PdhOpenQueryW: {status(code)}")
        self.counters = []

    def add(self, name, path, array=False, rate=False):
        handle = W.HANDLE()
        code = self.dll.PdhAddEnglishCounterW(self.query, path, 0, C.byref(handle))
        self.counters.append({"name": name, "path": path, "array": array,
                              "rate": rate, "handle": handle, "add_status": int(code)})

    def values(self, counter, first):
        result = {"path": counter["path"], "add_status": status(counter["add_status"])}
        if counter["add_status"]:
            result["error"] = "counter_add_failed"
            return result
        flags = PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
        if counter["array"]:
            size, count = W.DWORD(), W.DWORD()
            code = self.dll.PdhGetFormattedCounterArrayW(counter["handle"], flags, C.byref(size), C.byref(count), None)
            if code != PDH_MORE_DATA or not size.value:
                result.update(format_status=status(code), instances=[])
                if code:
                    result["error"] = "array_size_or_data_unavailable"
                return result
            # A counter's instance list can grow between sizing and retrieval.
            for _ in range(3):
                if size.value > 16 * 1024**2:
                    result.update(error="counter_array_exceeds_16MiB_budget", instances=[])
                    return result
                buf = C.create_string_buffer(size.value)
                code = self.dll.PdhGetFormattedCounterArrayW(counter["handle"], flags, C.byref(size), C.byref(count), buf)
                if code != PDH_MORE_DATA:
                    break
            result["format_status"] = status(code)
            if code:
                result.update(error="array_format_failed", instances=[])
                return result
            items = C.cast(buf, C.POINTER(FMT_ITEM))
            instances = []
            for i in range(count.value):
                item = items[i]
                code_status = int(item.FmtValue.CStatus)
                value = float(item.FmtValue.value.doubleValue)
                instances.append(format_instance(counter["name"], item.szName, value,
                                                  code_status, first and counter["rate"]))
            result["instances"] = instances
        else:
            value = FMT_VALUE()
            counter_type = W.DWORD()
            code = self.dll.PdhGetFormattedCounterValue(counter["handle"], flags, C.byref(counter_type), C.byref(value))
            number = float(value.value.doubleValue)
            valid = code == 0 and int(value.CStatus) in VALID_STATUSES and math.isfinite(number)
            result.update(format_status=status(code), cstatus=status(value.CStatus),
                          value=number if valid and not (first and counter["rate"]) else None,
                          valid=valid and not (first and counter["rate"]))
            if code:
                result["error"] = "counter_format_failed"
        if first and counter["rate"]:
            result["note"] = "first_rate_sample_requires_previous_collection"
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", required=True, help="Target PID, or self for a short test")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--duration-seconds", type=float, default=600)
    parser.add_argument("--interval-seconds", type=float, default=2)
    parser.add_argument("--deadline", help="Absolute ISO timestamp with timezone; default next local 00:10")
    args = parser.parse_args(argv)
    if args.duration_seconds <= 0 or args.interval_seconds <= 0:
        parser.error("Duration and interval must be positive")
    if args.interval_seconds < 1:
        parser.error("Minimum interval is one second")
    pid = os.getpid() if args.pid.lower() == "self" else int(args.pid)
    now = dt.datetime.now().astimezone()
    if args.deadline:
        deadline = dt.datetime.fromisoformat(args.deadline)
        if deadline.tzinfo is None:
            parser.error("--deadline must include an explicit timezone")
    else:
        deadline = now.replace(hour=0, minute=10, second=0, microsecond=0)
        if deadline <= now:
            deadline += dt.timedelta(days=1)
    if deadline <= now:
        parser.error("Deadline has already passed")
    pdh = PDH()
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.SetPriorityClass.argtypes = [W.HANDLE, W.DWORD]
    kernel.SetPriorityClass.restype = W.BOOL
    kernel.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
    kernel.GetExitCodeProcess.restype = W.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        pdh.dll.PdhCloseQuery(pdh.query)
        raise C.WinError(C.get_last_error())
    priority_ok = bool(kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000))
    process_prefix = f"\\GPU Process Memory(pid_{pid}_*)\\"
    pdh.add("gpu_process_dedicated_bytes", process_prefix + "Dedicated Usage", array=True)
    pdh.add("gpu_process_shared_bytes", process_prefix + "Shared Usage", array=True)
    pdh.add("gpu_process_total_committed_bytes", process_prefix + "Total Committed", array=True)
    pdh.add("gpu_engines_percent", f"\\GPU Engine(pid_{pid}_*)\\Utilization Percentage", array=True, rate=True)
    for name, counter in [("pages_input_per_sec", "Pages Input/sec"),
                          ("page_reads_per_sec", "Page Reads/sec"),
                          ("pages_output_per_sec", "Pages Output/sec"),
                          ("page_writes_per_sec", "Page Writes/sec")]:
        pdh.add(name, "\\Memory\\" + counter, rate=True)
    pdh.add("gpu_adapter_dedicated_bytes", "\\GPU Adapter Memory(*)\\Dedicated Usage", array=True)
    pdh.add("gpu_adapter_shared_bytes", "\\GPU Adapter Memory(*)\\Shared Usage", array=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    started, next_tick, samples = time.perf_counter(), time.perf_counter(), 0
    reason = "duration_completed"
    try:
        with args.out.open("x", encoding="utf-8") as output:
            def write(value):
                output.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
                output.flush()
            write({"kind": "metadata", "timestamp_utc": utc_now(), "target_pid": pid,
                   "collector_pid": os.getpid(), "process_access": "0x1000 query only",
                   "below_normal_priority": priority_ok, "duration_seconds": args.duration_seconds,
                   "interval_seconds": args.interval_seconds, "absolute_deadline": deadline.isoformat(),
                   "limitations": ["GPU engine instances are not summed.",
                                   "Single-engine GPU percentages outside 0..100.5 are invalid; raw values retained.",
                                   "System page reads include mapped-file reads and cannot identify per-game pagefile faults.",
                                   "First rate sample is null; API and counter statuses retained.",
                                   "GPU counters may be unavailable; each counter fails independently."]})
            try:
                while time.perf_counter() - started < args.duration_seconds:
                    if dt.datetime.now(dt.timezone.utc) >= deadline:
                        reason = "absolute_deadline"
                        break
                    exitcode = W.DWORD()
                    if kernel.GetExitCodeProcess(handle, C.byref(exitcode)) and exitcode.value != 259:
                        reason = "target_exited"
                        break
                    remaining = next_tick - time.perf_counter()
                    if remaining > 0:
                        time.sleep(min(remaining, .25))
                        continue
                    tick_started = time.perf_counter()
                    code = pdh.dll.PdhCollectQueryData(pdh.query)
                    counters = {}
                    for counter in pdh.counters:
                        try:
                            counters[counter["name"]] = pdh.values(counter, samples == 0)
                        except Exception as exc:
                            counters[counter["name"]] = {"error": type(exc).__name__, "message": str(exc)}
                    write({"kind": "sample", "timestamp_utc": utc_now(), "qpc_ns": time.perf_counter_ns(),
                           "elapsed_seconds": round(time.perf_counter() - started, 6), "sample": samples,
                           "collect_status": status(code), "counters": counters,
                           "collection_ms": round((time.perf_counter() - tick_started) * 1000, 4)})
                    samples += 1
                    next_tick += args.interval_seconds
                    if next_tick < time.perf_counter():
                        next_tick = time.perf_counter() + args.interval_seconds
            except KeyboardInterrupt:
                reason = "interrupted"
            finally:
                write({"kind": "finished", "timestamp_utc": utc_now(), "reason": reason,
                       "samples": samples, "elapsed_seconds": round(time.perf_counter() - started, 6)})
    finally:
        kernel.CloseHandle(handle)
        pdh.dll.PdhCloseQuery(pdh.query)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
