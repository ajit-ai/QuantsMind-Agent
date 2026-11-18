"""Process memory probe and the v0.10 memory-pressure baseline.

The product targets 4 GB Windows machines, so the workbench shows how much
memory the process is using and degrades visibly rather than thrashing. This
module provides the measurement and classification layer; subsystem-specific
eviction policies are added with the subsystems that own the memory.

No third-party dependency is used: Windows reads ``GetProcessMemoryInfo``,
Linux reads ``/proc/self/statm``, and macOS falls back to ``getrusage``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "PRESSURE_ORDER",
    "MemoryGovernor",
    "MemoryPressure",
    "MemorySnapshot",
]

MIB = 1024 * 1024
GIB = 1024 * MIB

#: ADR-012: typical ≤1.5 GB, heavy ≤2.5 GB, degrade at 3.0 GB, refuse new heavy
#: work at 3.2 GB. The process budget remains 4 GB.
TYPICAL_BYTES = int(1.5 * GIB)
HEAVY_BYTES = int(2.5 * GIB)
DEGRADE_BYTES = int(3.0 * GIB)
REFUSE_BYTES = int(3.2 * GIB)
BUDGET_BYTES = 4 * GIB


class MemoryPressure(StrEnum):
    NORMAL = "normal"
    ELEVATED = "elevated"
    CRITICAL = "critical"
    EXHAUSTED = "exhausted"


PRESSURE_ORDER: dict[MemoryPressure, int] = {
    MemoryPressure.NORMAL: 0,
    MemoryPressure.ELEVATED: 1,
    MemoryPressure.CRITICAL: 2,
    MemoryPressure.EXHAUSTED: 3,
}


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    working_set_bytes: int
    budget_bytes: int = BUDGET_BYTES
    pressure: MemoryPressure = MemoryPressure.NORMAL

    @property
    def working_set_mib(self) -> int:
        return round(self.working_set_bytes / MIB)

    @property
    def budget_mib(self) -> int:
        return round(self.budget_bytes / MIB)

    @property
    def available_bytes(self) -> int:
        return max(0, self.budget_bytes - self.working_set_bytes)

    def describe(self) -> str:
        return f"{self.working_set_mib} MB ({self.pressure.value})"


class MemoryGovernor:
    """Samples process memory and classifies pressure against the budget."""

    def __init__(self, *, budget_bytes: int = BUDGET_BYTES) -> None:
        self._budget = max(1, budget_bytes)

    # -- measurement -------------------------------------------------------
    def working_set_bytes(self) -> int:
        try:
            # Annotated so a type checker targeting one platform does not
            # constant-fold the other platform probes into "unreachable".
            platform: str = sys.platform
            if platform == "win32":
                return _windows_working_set()
            if platform.startswith("linux"):
                return _linux_working_set()
            return _posix_peak_working_set()
        except Exception:  # pragma: no cover - a probe must never break the UI
            return 0

    def snapshot(self) -> MemorySnapshot:
        used = self.working_set_bytes()
        return MemorySnapshot(
            working_set_bytes=used,
            budget_bytes=self._budget,
            pressure=self.classify(used),
        )

    def pressure(self) -> MemoryPressure:
        return self.classify(self.working_set_bytes())

    def classify(self, used_bytes: int) -> MemoryPressure:
        if used_bytes >= REFUSE_BYTES:
            return MemoryPressure.EXHAUSTED
        if used_bytes >= DEGRADE_BYTES:
            return MemoryPressure.CRITICAL
        if used_bytes >= TYPICAL_BYTES:
            return MemoryPressure.ELEVATED
        return MemoryPressure.NORMAL

    # -- policy hooks ------------------------------------------------------
    def may_start_heavy_feature(self) -> bool:
        """Whether a new heavy feature (LSP, terminal, large diff) may start."""

        return self.working_set_bytes() < REFUSE_BYTES

    def should_degrade(self) -> bool:
        """Whether ordered degradation should run."""

        return self.working_set_bytes() >= DEGRADE_BYTES


# -- platform probes -------------------------------------------------------
def _windows_working_set() -> int:
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    # GetCurrentProcess returns a full-width pseudo-handle; without an explicit
    # restype ctypes truncates it to 32 bits and the query fails silently.
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    probe = getattr(kernel32, "K32GetProcessMemoryInfo", None)
    if probe is None:
        probe = psapi.GetProcessMemoryInfo
    probe.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    probe.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    if not probe(handle, ctypes.byref(counters), counters.cb):
        return 0
    return int(counters.WorkingSetSize)


def _linux_working_set() -> int:
    with open("/proc/self/statm", encoding="ascii") as handle:
        fields = handle.read().split()
    resident_pages = int(fields[1])
    sysconf = getattr(os, "sysconf", None)
    page_size = int(sysconf("SC_PAGE_SIZE")) if sysconf is not None else 4096
    return resident_pages * page_size


def _posix_peak_working_set() -> int:
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows has no resource module
        return 0

    getrusage = getattr(resource, "getrusage", None)
    if getrusage is None:  # pragma: no cover - defensive
        return 0
    usage = int(getrusage(getattr(resource, "RUSAGE_SELF", 0)).ru_maxrss)
    # macOS reports bytes; Linux reports kibibytes. Use the platform to decide.
    return usage if sys.platform == "darwin" else usage * 1024
