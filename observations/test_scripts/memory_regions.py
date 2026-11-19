"""Read-only Windows x64 memory-region diagnostics; Python standard library only.

No memory contents, writes, injection, suspension, trimming or debug privileges.
Region spans and sampled residency do NOT identify application objects, leaks,
page age, or objects that can safely be freed. MEM_IMAGE/MEM_MAPPED may contain
private copy-on-write pages. Sample counts are not exact resident byte totals.

The two-second deep-scan budget is cooperative, checked between API calls; it
cannot interrupt a kernel call already in progress. Snapshots are non-atomic.
"""

import argparse
import bisect
import ctypes as C
from ctypes import wintypes as W
import datetime as dt
import gc
import json
import os
from pathlib import Path
import random
import sys
import time


MEM_COMMIT, MEM_RESERVE, MEM_FREE = 0x1000, 0x2000, 0x10000
MEM_PRIVATE, MEM_MAPPED, MEM_IMAGE = 0x20000, 0x40000, 0x1000000
QUERY_INFORMATION = 0x0400
SCAN_BUDGET_SECONDS = 2.0
MIN_AVAILABLE_BYTES = 2 * 1024**3
MAX_REGIONS = 100000
TYPE_NAMES = {MEM_PRIVATE: "MEM_PRIVATE", MEM_MAPPED: "MEM_MAPPED", MEM_IMAGE: "MEM_IMAGE"}


class MEMORY_BASIC_INFORMATION(C.Structure):
    _fields_ = [("BaseAddress", C.c_void_p), ("AllocationBase", C.c_void_p),
                ("AllocationProtect", W.DWORD), ("PartitionId", W.WORD),
                ("RegionSize", C.c_size_t), ("State", W.DWORD),
                ("Protect", W.DWORD), ("Type", W.DWORD)]


class PROCESS_MEMORY_COUNTERS_EX(C.Structure):
    _fields_ = [("cb", W.DWORD), ("PageFaultCount", W.DWORD)] + [
        (name, C.c_size_t) for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
            "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]


class MEMORYSTATUSEX(C.Structure):
    _fields_ = [("dwLength", W.DWORD), ("dwMemoryLoad", W.DWORD)] + [
        (name, C.c_ulonglong) for name in (
            "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
            "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]


class PERFORMANCE_INFORMATION(C.Structure):
    _fields_ = [("cb", W.DWORD)] + [(name, C.c_size_t) for name in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
        "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + [
        ("HandleCount", W.DWORD), ("ProcessCount", W.DWORD), ("ThreadCount", W.DWORD)]


class SYSTEM_INFO(C.Structure):
    _fields_ = [("wProcessorArchitecture", W.WORD), ("wReserved", W.WORD),
                ("dwPageSize", W.DWORD), ("lpMinimumApplicationAddress", C.c_void_p),
                ("lpMaximumApplicationAddress", C.c_void_p), ("dwActiveProcessorMask", C.c_size_t),
                ("dwNumberOfProcessors", W.DWORD), ("dwProcessorType", W.DWORD),
                ("dwAllocationGranularity", W.DWORD), ("wProcessorLevel", W.WORD),
                ("wProcessorRevision", W.WORD)]


class WS_EX_INFORMATION(C.Structure):
    _fields_ = [("VirtualAddress", C.c_void_p), ("VirtualAttributes", C.c_size_t)]


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def redact(value):
    profile = os.environ.get("USERPROFILE", "")
    if profile:
        # Windows paths are case-insensitive. Preserve the rest of the path.
        start = value.lower().find(profile.lower())
        if start >= 0:
            value = value[:start] + "<USERPROFILE>" + value[start + len(profile):]
        # GetMappedFileName returns NT device paths, which omit the drive letter.
        relative = profile[2:] if len(profile) > 2 and profile[1] == ":" else ""
        if relative:
            start = value.lower().find(relative.lower())
            if start >= 0:
                value = value[:start] + "<USERPROFILE>" + value[start + len(relative):]
    return value


def error_text():
    code = C.get_last_error()
    return {"winerror": code, "message": redact(C.FormatError(code).strip())}


class Windows:
    def __init__(self):
        if sys.platform != "win32" or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("Requires 64-bit Python on Windows.")
        expected = {MEMORY_BASIC_INFORMATION: 48, PROCESS_MEMORY_COUNTERS_EX: 80,
                    MEMORYSTATUSEX: 64, PERFORMANCE_INFORMATION: 104,
                    SYSTEM_INFO: 48, WS_EX_INFORMATION: 16}
        for struct, size in expected.items():
            if C.sizeof(struct) != size:
                raise RuntimeError(f"Unexpected layout: {struct.__name__}={C.sizeof(struct)}, expected {size}")
        if MEMORY_BASIC_INFORMATION.RegionSize.offset != 24:
            raise RuntimeError("Unexpected MEMORY_BASIC_INFORMATION alignment")
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.p = C.WinDLL("psapi", use_last_error=True)
        self.k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.k.OpenProcess.restype = W.HANDLE
        self.k.CloseHandle.argtypes = [W.HANDLE]
        self.k.CloseHandle.restype = W.BOOL
        self.k.GetCurrentProcess.argtypes = []
        self.k.GetCurrentProcess.restype = W.HANDLE
        self.k.VirtualQueryEx.argtypes = [W.HANDLE, C.c_void_p, C.POINTER(MEMORY_BASIC_INFORMATION), C.c_size_t]
        self.k.VirtualQueryEx.restype = C.c_size_t
        self.k.GlobalMemoryStatusEx.argtypes = [C.POINTER(MEMORYSTATUSEX)]
        self.k.GlobalMemoryStatusEx.restype = W.BOOL
        self.k.GetNativeSystemInfo.argtypes = [C.POINTER(SYSTEM_INFO)]
        self.k.GetNativeSystemInfo.restype = None
        self.k.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
        self.k.GetProcessTimes.restype = W.BOOL
        self.k.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
        self.k.GetExitCodeProcess.restype = W.BOOL
        self.k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        self.k.QueryFullProcessImageNameW.restype = W.BOOL
        self.p.GetProcessMemoryInfo.argtypes = [W.HANDLE, C.POINTER(PROCESS_MEMORY_COUNTERS_EX), W.DWORD]
        self.p.GetProcessMemoryInfo.restype = W.BOOL
        self.p.GetPerformanceInfo.argtypes = [C.POINTER(PERFORMANCE_INFORMATION), W.DWORD]
        self.p.GetPerformanceInfo.restype = W.BOOL
        self.p.GetMappedFileNameW.argtypes = [W.HANDLE, C.c_void_p, W.LPWSTR, W.DWORD]
        self.p.GetMappedFileNameW.restype = W.DWORD
        self.p.QueryWorkingSetEx.argtypes = [W.HANDLE, C.c_void_p, W.DWORD]
        self.p.QueryWorkingSetEx.restype = W.BOOL
        self.info = SYSTEM_INFO()
        self.k.GetNativeSystemInfo(C.byref(self.info))
        self.page_size = int(self.info.dwPageSize)

    def process_memory(self, handle):
        pm = PROCESS_MEMORY_COUNTERS_EX()
        pm.cb = C.sizeof(pm)
        if not self.p.GetProcessMemoryInfo(handle, C.byref(pm), C.sizeof(pm)):
            return {"error": error_text()}
        result = {"private_commit_bytes": int(pm.PrivateUsage), "working_set_bytes": int(pm.WorkingSetSize),
                "peak_working_set_bytes": int(pm.PeakWorkingSetSize),
                "page_fault_count_including_soft": int(pm.PageFaultCount)}
        times = [W.FILETIME() for _ in range(4)]
        if self.k.GetProcessTimes(handle, *(C.byref(x) for x in times)):
            result["cpu_seconds"] = sum(
                int(x.dwLowDateTime) | (int(x.dwHighDateTime) << 32)
                for x in times[2:]
            ) / 10_000_000
        return result

    def baseline(self, handle):
        result = {"timestamp_utc": utc_now(), "qpc_ns": time.perf_counter_ns(),
                  "target": self.process_memory(handle),
                  "diagnostic_process": self.process_memory(self.k.GetCurrentProcess())}
        mem = MEMORYSTATUSEX()
        mem.dwLength = C.sizeof(mem)
        if self.k.GlobalMemoryStatusEx(C.byref(mem)):
            result["system"] = {"available_physical_bytes": int(mem.ullAvailPhys),
                                "total_physical_bytes": int(mem.ullTotalPhys),
                                "memory_load_percent": int(mem.dwMemoryLoad)}
        else:
            result["system"] = {"error": error_text()}
        perf = PERFORMANCE_INFORMATION()
        perf.cb = C.sizeof(perf)
        if self.p.GetPerformanceInfo(C.byref(perf), C.sizeof(perf)):
            page = int(perf.PageSize)
            result["system"].update({"commit_bytes": int(perf.CommitTotal) * page,
                                     "commit_limit_bytes": int(perf.CommitLimit) * page,
                                     "commit_headroom_bytes": int(perf.CommitLimit - perf.CommitTotal) * page,
                                     "kernel_paged_bytes": int(perf.KernelPaged) * page,
                                     "kernel_nonpaged_bytes": int(perf.KernelNonpaged) * page})
        else:
            result["system"]["performance_error"] = error_text()
        return result

    def mapped_name(self, handle, address):
        buf = C.create_unicode_buffer(32768)
        n = self.p.GetMappedFileNameW(handle, C.c_void_p(address), buf, len(buf))
        if not n:
            return None, error_text()
        return redact(buf.value), None


def sample_counter():
    return {"queried_samples": 0, "valid_samples": 0, "nonresident_or_invalid_samples": 0,
            "shareable_valid_samples": 0, "private_valid_samples": 0,
            "locked_valid_samples": 0, "large_page_valid_samples": 0}


def count_flags(counter, flags):
    counter["queried_samples"] += 1
    if not (flags & 1):
        counter["nonresident_or_invalid_samples"] += 1
        return
    counter["valid_samples"] += 1
    counter["shareable_valid_samples" if flags & (1 << 15) else "private_valid_samples"] += 1
    counter["locked_valid_samples"] += bool(flags & (1 << 22))
    counter["large_page_valid_samples"] += bool(flags & (1 << 23))


def region_json(region):
    """Expand one compact internal record only while serializing it."""
    base, size, state, allocation, kind, allocation_protect, protect = region
    row = {"base_address": hex(base), "region_span_bytes": size, "state": state}
    if state != MEM_FREE:
        row.update(allocation_base=hex(allocation), type=kind,
                   type_name=TYPE_NAMES.get(kind, hex(kind)),
                   allocation_protect=allocation_protect)
        if state == MEM_COMMIT:
            row["protect"] = protect
    return row


def scan(api, handle, max_pages):
    started = time.perf_counter()
    deadline = started + SCAN_BUDGET_SECONDS
    result = {"timestamp_utc": utc_now(), "qpc_ns": time.perf_counter_ns(),
              "complete": True, "budget_seconds": SCAN_BUDGET_SECONDS,
              "sample_counts_are_not_byte_estimates": True, "errors": [],
              "diagnostic_process_before": api.process_memory(api.k.GetCurrentProcess())}
    regions, groups, spans = [], {}, {}
    address, limit = int(api.info.lpMinimumApplicationAddress or 0), int(api.info.lpMaximumApplicationAddress or 0)
    committed = []
    while address <= limit:
        if time.perf_counter() >= deadline or len(regions) >= MAX_REGIONS:
            result.update(complete=False, abort_reason="time_or_region_budget", next_address=hex(address))
            break
        mbi = MEMORY_BASIC_INFORMATION()
        got = api.k.VirtualQueryEx(handle, C.c_void_p(address), C.byref(mbi), C.sizeof(mbi))
        if got != C.sizeof(mbi):
            result["errors"].append({"api": "VirtualQueryEx", "address": hex(address), **error_text()})
            result.update(complete=False, abort_reason="VirtualQueryEx_failed", next_address=hex(address))
            break
        base, size, state = int(mbi.BaseAddress or 0), int(mbi.RegionSize), int(mbi.State)
        if size <= 0 or base + size <= address:
            result.update(complete=False, abort_reason="nonadvancing_region", next_address=hex(address))
            break
        alloc, kind, allocation_protect, protect = 0, 0, 0, 0
        if state != MEM_FREE:
            alloc, kind, protect = int(mbi.AllocationBase or 0), int(mbi.Type), int(mbi.Protect)
            allocation_protect = int(mbi.AllocationProtect)
            key = (alloc, kind)
            if key not in groups:
                groups[key] = {"allocation_base": hex(alloc), "type": kind,
                               "type_name": TYPE_NAMES.get(kind, hex(kind)), "region_count": 0,
                               "virtual_committed_span_bytes": 0, "reserved_span_bytes": 0,
                               "committed_spans_by_protect": {}, "sample_counts": sample_counter()}
            group = groups[key]
            group["region_count"] += 1
            if state == MEM_COMMIT:
                group["virtual_committed_span_bytes"] += size
                group["committed_spans_by_protect"][hex(protect)] = group["committed_spans_by_protect"].get(hex(protect), 0) + size
                spans[TYPE_NAMES.get(kind, hex(kind))] = spans.get(TYPE_NAMES.get(kind, hex(kind)), 0) + size
                committed.append((base, size // api.page_size, key))
                if kind in (MEM_MAPPED, MEM_IMAGE) and "mapped_name" not in group:
                    if time.perf_counter() < deadline:
                        name, error = api.mapped_name(handle, base)
                        group["mapped_name"] = name
                        if error:
                            group["mapped_name_error"] = error
            elif state == MEM_RESERVE:
                group["reserved_span_bytes"] += size
        # Keep numeric tuples instead of tens of thousands of dictionaries and
        # repeated hexadecimal strings. Public JSON fields stay unchanged.
        regions.append((base, size, state, alloc, kind, allocation_protect, protect))
        address = base + size
    result["region_count"] = len(regions)
    result["virtual_committed_spans_by_type"] = spans
    result["working_set_sample_counts"] = sample_counter()
    total_pages = sum(x[1] for x in committed)
    n = min(max_pages, total_pages)
    result["eligible_committed_pages_in_scanned_regions"] = total_pages
    result["planned_sample_count"] = n
    result["sampling_method"] = "fixed-seed one random page per equal stratum of committed address spans"
    if n and time.perf_counter() < deadline:
        cumulative, cumulative_count = [], 0
        for _, count, _ in committed:
            cumulative_count += count
            cumulative.append(cumulative_count)
        rng = random.Random(0x5959534C53)
        selected = []
        for i in range(n):
            lo, hi = i * total_pages // n, (i + 1) * total_pages // n
            page_index = rng.randrange(lo, hi)
            pos = bisect.bisect_right(cumulative, page_index)
            previous = cumulative[pos - 1] if pos else 0
            base, _, key = committed[pos]
            selected.append((base + (page_index - previous) * api.page_size, key))
        for offset in range(0, len(selected), 2048):
            if time.perf_counter() >= deadline:
                result.update(complete=False, sample_abort_reason="time_budget")
                break
            chunk = selected[offset:offset + 2048]
            block = (WS_EX_INFORMATION * len(chunk))()
            for i, (va, _) in enumerate(chunk):
                block[i].VirtualAddress = va
            if not api.p.QueryWorkingSetEx(handle, C.byref(block), C.sizeof(block)):
                result["errors"].append({"api": "QueryWorkingSetEx", **error_text()})
                result.update(complete=False, sample_abort_reason="QueryWorkingSetEx_failed")
                break
            for entry, (_, key) in zip(block, chunk):
                count_flags(result["working_set_sample_counts"], int(entry.VirtualAttributes))
                count_flags(groups[key]["sample_counts"], int(entry.VirtualAttributes))
    elif n:
        result.update(complete=False, sample_abort_reason="time_budget_before_sampling")
    result["allocations"] = sorted(groups.values(), key=lambda x: x["virtual_committed_span_bytes"], reverse=True)
    result["scan_elapsed_seconds"] = round(time.perf_counter() - started, 6)
    result["budget_exceeded"] = result["scan_elapsed_seconds"] > SCAN_BUDGET_SECONDS
    result["diagnostic_process_after"] = api.process_memory(api.k.GetCurrentProcess())
    return result, regions


def write_line(file, value):
    file.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    file.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", required=True, help="Target PID, or 'self' for a self-test")
    parser.add_argument("--out", required=True, type=Path, help="New diagnostic session directory")
    parser.add_argument("--duration-seconds", type=float, default=3600)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--map-interval-seconds", type=float, default=120)
    parser.add_argument("--max-pages", type=int, default=16384)
    args = parser.parse_args(argv)
    if min(args.duration_seconds, args.interval_seconds, args.map_interval_seconds) <= 0:
        parser.error("All durations and intervals must be positive")
    if not 0 <= args.max_pages <= 16384:
        parser.error("--max-pages must be between 0 and 16384")
    pid = os.getpid() if args.pid.lower() == "self" else int(args.pid)
    api = Windows()
    handle = api.k.OpenProcess(QUERY_INFORMATION, False, pid)
    if not handle:
        raise OSError(f"OpenProcess(0x0400) failed: {error_text()}")
    try:
        # An open handle keeps its identity even if the PID is later reused.
        times = [W.FILETIME() for _ in range(4)]
        creation = None
        if api.k.GetProcessTimes(handle, *(C.byref(x) for x in times)):
            creation = int(times[0].dwLowDateTime) | (int(times[0].dwHighDateTime) << 32)
        image, length = C.create_unicode_buffer(32768), W.DWORD(32768)
        image_name = redact(image.value) if api.k.QueryFullProcessImageNameW(handle, 0, image, C.byref(length)) else None
        # Refuse to overwrite another run or mix sessions.
        args.out.mkdir(parents=True, exist_ok=False)
        metadata = {"started_utc": utc_now(), "pid": pid, "target_image": image_name,
                    "process_creation_filetime": creation, "access_mask": "0x0400",
                    "python_pid": os.getpid(), "page_size": api.page_size,
                    "duration_seconds": args.duration_seconds, "interval_seconds": args.interval_seconds,
                    "map_interval_seconds": args.map_interval_seconds, "max_pages": args.max_pages,
                    "deep_scan_budget_seconds": SCAN_BUDGET_SECONDS,
                    "skip_deep_if_available_below_bytes": MIN_AVAILABLE_BYTES,
                    "limitations": ["No memory contents read; no target modifications.",
                                    "Region spans are not application object sizes or exact private commitment.",
                                    "Residency samples are not exact bytes, access age or reclaimability.",
                                    "Copy-on-write pages retain MEM_IMAGE/MEM_MAPPED types.",
                                    "Concurrent allocations make snapshots non-atomic; addresses may be reused.",
                                    "Two-second budget is cooperative; a running API call cannot be interrupted."]}
        (args.out / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        started = time.perf_counter()
        next_tick, next_map, sample_id, scan_id = started, started, 0, 0
        reason = "duration_completed"
        try:
            with (args.out / "baseline.jsonl").open("x", encoding="utf-8") as baseline_file, (args.out / "scans.jsonl").open("x", encoding="utf-8") as scans_file:
                while time.perf_counter() - started < args.duration_seconds:
                    if (args.out / "stop.request").exists():
                        reason = "stop_requested"
                        break
                    now = time.perf_counter()
                    if now < next_tick:
                        time.sleep(min(next_tick - now, .25))
                        continue
                    code = W.DWORD()
                    if api.k.GetExitCodeProcess(handle, C.byref(code)) and code.value != 259:
                        reason = "target_exited"
                        break
                    sample = api.baseline(handle)
                    sample.update(sample=sample_id, elapsed_seconds=round(time.perf_counter() - started, 6))
                    sample_id += 1
                    write_line(baseline_file, sample)
                    if now >= next_map:
                        available = sample.get("system", {}).get("available_physical_bytes")
                        if available is None or available < MIN_AVAILABLE_BYTES:
                            write_line(scans_file, {"timestamp_utc": utc_now(), "skipped": True,
                                                   "reason": "available_below_2GiB_or_unknown",
                                                   "available_physical_bytes": available})
                        else:
                            summary, regions = scan(api, handle, args.max_pages)
                            summary["scan"] = scan_id
                            filename = f"regions-{scan_id:05d}.jsonl"
                            io_started = time.perf_counter()
                            with (args.out / filename).open("x", encoding="utf-8") as f:
                                for region in regions:
                                    f.write(json.dumps(region_json(region), ensure_ascii=False, separators=(",", ":")) + "\n")
                            summary["regions_file"] = filename
                            summary["regions_write_seconds"] = round(time.perf_counter() - io_started, 6)
                            write_line(scans_file, summary)
                            print(json.dumps({"scan": scan_id, "complete": summary["complete"],
                                              "regions": summary["region_count"],
                                              "queried_pages": summary["working_set_sample_counts"]["queried_samples"],
                                              "scan_seconds": summary["scan_elapsed_seconds"]}), flush=True)
                            scan_id += 1
                            # Do not retain the last large snapshot throughout
                            # the two-minute idle interval. No process trimming.
                            del summary, regions
                            gc.collect()
                        next_map = time.perf_counter() + args.map_interval_seconds
                    next_tick += args.interval_seconds
                    if next_tick < time.perf_counter():
                        next_tick = time.perf_counter() + args.interval_seconds
        except KeyboardInterrupt:
            reason = "interrupted"
        except Exception:
            reason = "error"
            raise
        finally:
            (args.out / "finished.json").write_text(json.dumps({"finished_utc": utc_now(), "reason": reason,
                "elapsed_seconds": round(time.perf_counter() - started, 6), "baseline_samples": sample_id,
                "deep_scans": scan_id}, indent=2), encoding="utf-8")
    finally:
        api.k.CloseHandle(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
