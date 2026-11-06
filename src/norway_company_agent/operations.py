from __future__ import annotations

import math
try:
    import resource
except ImportError:  # Windows
    resource = None
import sys
from collections import Counter
from typing import Iterable


def percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def latency_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    measured = list(values)
    return {
        "n": len(measured),
        "p50_ms": round(percentile(measured, 0.50), 3) if measured else None,
        "p95_ms": round(percentile(measured, 0.95), 3) if measured else None,
        "max_ms": round(max(measured), 3) if measured else None,
    }


def domain_request_summary(counts: Counter[str]) -> dict[str, float | int | None]:
    values = list(counts.values())
    return {
        "domains": len(values),
        "p50_requests": percentile(values, 0.50),
        "p95_requests": percentile(values, 0.95),
        "max_requests": max(values) if values else None,
    }


def peak_rss_bytes() -> int:
    if resource is None:
        import ctypes
        from ctypes import wintypes
        class MemoryCounters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        *[(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = (wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD)
        if not get_memory(current(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return int(counters.PeakWorkingSetSize)
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux and most BSD-derived CI images report KiB.
    return int(value if sys.platform == "darwin" else value * 1024)
