"""Read-only host-process detection for conservative VHDX lifecycle decisions."""

from __future__ import annotations

import ntpath
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class HostProcessState(StrEnum):
    """Tri-state observation used to avoid detach on uncertain host state."""

    RUNNING = "running"
    NOT_RUNNING = "not_running"
    UNKNOWN = "unknown"


class TraktorHost(StrEnum):
    """The explicitly recognized Traktor hosts."""

    TRAKTOR_PRO_2 = "TRAKTOR_PRO_2"
    TRAKTOR_PRO_3 = "TRAKTOR_PRO_3"
    TRAKTOR_PRO_4 = "TRAKTOR_PRO_4"


@dataclass(frozen=True, slots=True)
class HostExecutableSpecification:
    """One exact host executable and its optional full-path allowlist."""

    host: TraktorHost
    process_name: str
    allowed_executable_paths: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class WindowsProcessEntry:
    """The PID and executable basename supplied by Toolhelp."""

    pid: int
    process_name: str


def _normalize_windows_executable_path(path: str) -> str:
    """Canonicalize one absolute Windows path for an exact comparison."""
    if not isinstance(path, str) or not ntpath.isabs(path):
        raise ValueError("process image path must be an absolute Windows path")
    return ntpath.normpath(path).casefold()


TRAKTOR_HOST_EXECUTABLES = (
    HostExecutableSpecification(
        TraktorHost.TRAKTOR_PRO_2,
        "Traktor.exe",
        frozenset(
            {
                _normalize_windows_executable_path(
                    r"C:\Program Files\Native Instruments\Traktor Pro 2\Traktor.exe"
                )
            }
        ),
    ),
    HostExecutableSpecification(
        TraktorHost.TRAKTOR_PRO_3,
        "Traktor.exe",
        frozenset(
            {
                _normalize_windows_executable_path(
                    r"C:\Program Files\Native Instruments\Traktor Pro 3\Traktor.exe"
                )
            }
        ),
    ),
    HostExecutableSpecification(TraktorHost.TRAKTOR_PRO_4, "Traktor Pro 4.exe"),
)
TRAKTOR_EXECUTABLE_NAMES = frozenset(
    specification.process_name.casefold()
    for specification in TRAKTOR_HOST_EXECUTABLES
)


class HostProcessProbe(Protocol):
    """Small injectable surface for observing the protected host."""

    def state(self) -> HostProcessState: ...


def _windows_process_entries() -> tuple[WindowsProcessEntry, ...]:
    """Enumerate executable basenames and PIDs through read-only Toolhelp."""
    if os.name != "nt":
        raise OSError("Windows process enumeration is unavailable")

    import ctypes
    from ctypes import wintypes

    max_path = 260
    snapshot_processes = 0x00000002
    error_no_more_files = 18
    invalid_handle_value = ctypes.c_void_p(-1).value

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * max_path),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(snapshot_processes, 0)
    if snapshot == invalid_handle_value:
        raise ctypes.WinError(ctypes.get_last_error())

    entries: list[WindowsProcessEntry] = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            error = ctypes.get_last_error()
            if error != error_no_more_files:
                raise ctypes.WinError(error)
            return ()
        while True:
            entries.append(WindowsProcessEntry(entry.th32ProcessID, entry.szExeFile))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error != error_no_more_files:
                    raise ctypes.WinError(error)
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(entries)


def _windows_process_image_path(pid: int) -> str:
    """Read a process image path with query-only access and close its handle."""
    if os.name != "nt":
        raise OSError("Windows process inspection is unavailable")

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    image_path_buffer_size = 32768
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        image_path = ctypes.create_unicode_buffer(image_path_buffer_size)
        size = wintypes.DWORD(len(image_path))
        if not kernel32.QueryFullProcessImageNameW(
            process,
            0,
            image_path,
            ctypes.byref(size),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return image_path.value
    finally:
        kernel32.CloseHandle(process)


class WindowsTraktorProcessProbe:
    """Recognize only the explicit Traktor host executable catalog."""

    def __init__(
        self,
        enumerate_processes: Callable[[], Iterable[WindowsProcessEntry]] = (
            _windows_process_entries
        ),
        query_process_image_path: Callable[[int], str] = _windows_process_image_path,
        specifications: Iterable[HostExecutableSpecification] = TRAKTOR_HOST_EXECUTABLES,
    ) -> None:
        self._enumerate_processes = enumerate_processes
        self._query_process_image_path = query_process_image_path
        self._specifications = tuple(specifications)
        self.detected_host: TraktorHost | None = None
        self._diagnostic: dict[str, object] = {}

    def diagnostic(self) -> Mapping[str, object]:
        """Return the last conservative observation failure, if any."""
        return dict(self._diagnostic)

    def _unknown(self, stage: str, exc: Exception, **details: object) -> None:
        self._diagnostic = {
            "stage": stage,
            "error": type(exc).__name__,
            "message": str(exc),
            **details,
        }

    def state(self) -> HostProcessState:
        self.detected_host = None
        self._diagnostic = {}
        try:
            entries = tuple(self._enumerate_processes())
        except Exception as exc:
            self._unknown("PROCESS_ENUMERATION", exc)
            return HostProcessState.UNKNOWN

        specifications_by_name: dict[str, tuple[HostExecutableSpecification, ...]] = {}
        for specification in self._specifications:
            name = specification.process_name.casefold()
            specifications_by_name[name] = specifications_by_name.get(name, ()) + (
                specification,
            )

        path_query_failed = False
        for entry in entries:
            if not isinstance(entry, WindowsProcessEntry):
                continue
            specifications = specifications_by_name.get(entry.process_name.casefold())
            if specifications is None:
                continue
            no_path_specification = next(
                (
                    specification
                    for specification in specifications
                    if not specification.allowed_executable_paths
                ),
                None,
            )
            if no_path_specification is not None:
                self.detected_host = no_path_specification.host
                return HostProcessState.RUNNING
            try:
                image_path = _normalize_windows_executable_path(
                    self._query_process_image_path(entry.pid)
                )
            except Exception as exc:
                path_query_failed = True
                self._unknown(
                    "QUERY_FULL_PROCESS_IMAGE_NAME",
                    exc,
                    pid=entry.pid,
                    process_name=entry.process_name,
                )
                continue
            for specification in specifications:
                if image_path in specification.allowed_executable_paths:
                    self.detected_host = specification.host
                    return HostProcessState.RUNNING

        return (
            HostProcessState.UNKNOWN
            if path_query_failed
            else HostProcessState.NOT_RUNNING
        )
