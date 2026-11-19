"""Query public allocation metadata for supplied addresses on Windows x64.

Uses OpenProcess(PROCESS_QUERY_INFORMATION) and QueryVirtualMemoryInformation
(MemoryRegionInfo), not content reads, page touches, enumeration or modification.
CommitSize is the API's allocation commit charge; it is not resident bytes or
reclaimable bytes. Protection/flags cannot identify game objects or safe frees.
Only the six documented flag fields are decoded; reserved bits are not inferred.

Self-test: python observations/test_scripts/query_region_info.py --pid self --addresses self
Docs: https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-queryvirtualmemoryinformation
      https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/ns-memoryapi-win32_memory_region_information
"""

import argparse
import ctypes as C
from ctypes import wintypes as W
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time


class RegionFlags(C.LittleEndianStructure):
    _fields_ = [(name, C.c_uint32, 1) for name in (
        "Private", "MappedDataFile", "MappedImage", "MappedPageFile",
        "MappedPhysical", "DirectMapped")] + [("Reserved", C.c_uint32, 26)]


class RegionFlagsUnion(C.Union):
    _fields_ = [("raw", C.c_uint32), ("bits", RegionFlags)]


class WIN32_MEMORY_REGION_INFORMATION(C.Structure):
    _fields_ = [("AllocationBase", C.c_void_p), ("AllocationProtect", C.c_uint32),
                ("flags", RegionFlagsUnion), ("RegionSize", C.c_size_t),
                ("CommitSize", C.c_size_t)]


FLAG_NAMES = [name for name, *_ in RegionFlags._fields_ if name != "Reserved"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", required=True, help="PID, or self")
    parser.add_argument("--addresses", required=True, nargs="+",
                        help="Decimal/0x addresses, separated by spaces or commas; self only for self-test")
    parser.add_argument("--out", type=Path, help="Optional new JSON file; existing files are never overwritten")
    args = parser.parse_args(argv)
    if sys.platform != "win32" or C.sizeof(C.c_void_p) != 8:
        parser.error("Requires 64-bit Python on Windows")
    if (C.sizeof(RegionFlags) != 4 or C.sizeof(WIN32_MEMORY_REGION_INFORMATION) != 32
            or WIN32_MEMORY_REGION_INFORMATION.flags.offset != 12
            or WIN32_MEMORY_REGION_INFORMATION.RegionSize.offset != 16
            or WIN32_MEMORY_REGION_INFORMATION.CommitSize.offset != 24):
        raise RuntimeError("Unexpected public structure layout")
    pid = os.getpid() if args.pid.lower() == "self" else int(args.pid)
    tokens = [part.strip() for token in args.addresses for part in token.split(",") if part.strip()]
    if not tokens or len(tokens) > 256:
        parser.error("Supply between 1 and 256 addresses")
    k = C.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]
    k.CloseHandle.restype = W.BOOL
    # This API is not exported from kernel32 on this machine. Load its public
    # API-set contract rather than falling back to undocumented NT interfaces.
    memory_api = C.WinDLL("api-ms-win-core-memory-l1-1-4.dll", use_last_error=True)
    query = memory_api.QueryVirtualMemoryInformation
    query.argtypes = [W.HANDLE, C.c_void_p, C.c_int, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
    query.restype = W.BOOL
    keep_alive = None
    if tokens == ["self"]:
        if pid != os.getpid():
            parser.error("--addresses self is valid only for the current Python process")
        # Existing Python-owned bytes plus an existing DLL code address. Neither
        # region is read, written or made resident by the query itself.
        keep_alive = C.create_string_buffer(4096)
        addresses = [C.addressof(keep_alive), C.cast(query, C.c_void_p).value]
    else:
        try:
            addresses = [int(token, 0) for token in tokens]
        except ValueError:
            parser.error("Each address must be decimal or 0x-prefixed hexadecimal")
        if any(address < 0 or address > 0xFFFFFFFFFFFFFFFF for address in addresses):
            parser.error("Addresses must fit an unsigned 64-bit pointer")
    handle = k.OpenProcess(0x0400, False, pid)
    if not handle:
        raise C.WinError(C.get_last_error())
    started = time.perf_counter()
    results = []
    try:
        for address in addresses:
            info = WIN32_MEMORY_REGION_INFORMATION()
            returned = C.c_size_t()
            C.set_last_error(0)
            ok = query(handle, C.c_void_p(address), 0, C.byref(info), C.sizeof(info), C.byref(returned))
            row = {"requested_address": hex(address), "success": bool(ok),
                   "returned_structure_bytes": int(returned.value)}
            if ok:
                row.update(allocation_base=hex(info.AllocationBase or 0),
                           allocation_protect=hex(info.AllocationProtect),
                           page_writecombine=bool(info.AllocationProtect & 0x400),
                           page_nocache=bool(info.AllocationProtect & 0x200),
                           allocation_region_bytes=int(info.RegionSize),
                           allocation_commit_charge_bytes=int(info.CommitSize),
                           flags={name: bool(getattr(info.flags.bits, name)) for name in FLAG_NAMES})
                if returned.value != C.sizeof(info):
                    row["warning"] = "Unexpected returned structure size; verify before interpretation"
            else:
                code = C.get_last_error()
                row.update(winerror=code, error=C.FormatError(code).strip())
            results.append(row)
    finally:
        k.CloseHandle(handle)
    result = {"timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "pid": pid,
              "access_mask": "0x0400", "memory_information_class": "MemoryRegionInfo (0)",
              "api_contract": "api-ms-win-core-memory-l1-1-4.dll",
              "elapsed_ms": round((time.perf_counter() - started) * 1000, 4),
              "results": results,
              "limitations": ["Metadata only; no content access or target modifications.",
                              "CommitSize is allocation commit charge, not physical residency or safe-to-free memory.",
                              "MappedPhysical means a view of Device PhysicalMemory, not all GPU-related mappings.",
                              "DirectMapped does not identify GPU ownership.",
                              "Write-combine protection alone does not establish resource type, driver ownership or leak.",
                              "Multiple input addresses can refer to one allocation; do not sum duplicates."]}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8") as file:
            file.write(text + "\n")
    print(text)
    return 0 if all(row["success"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
