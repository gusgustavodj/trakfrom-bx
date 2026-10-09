"""Resolve storage tools from Windows APIs, never from a process search path."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import stat
import sys


class NativeWindowsToolUnavailableError(RuntimeError):
    """The expected native Windows tool could not be positively identified."""


def _windows_directories() -> tuple[Path, Path]:
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise NativeWindowsToolUnavailableError("native 64-bit Windows is required")
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    directories = []
    for name in ("GetSystemWindowsDirectoryW", "GetSystemDirectoryW"):
        function = getattr(kernel, name)
        function.argtypes = (wintypes.LPWSTR, wintypes.UINT)
        function.restype = wintypes.UINT
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(buffer, len(buffer))
        if not 0 < length < len(buffer):
            raise NativeWindowsToolUnavailableError("Windows directory query failed")
        directories.append(Path(buffer.value))
    return directories[0], directories[1]


def _checked_directories() -> tuple[Path, Path]:
    windows, system = _windows_directories()
    if not windows.is_absolute() or not system.is_absolute() or system != windows / "System32":
        raise NativeWindowsToolUnavailableError("unexpected native Windows directory")
    return windows, system


def resolve_native_tool(tool: str, configured: str | None = None) -> str:
    """Accept only the exact native location, with no reparse point in its path."""
    try:
        _, system = _checked_directories()
        relative = {
            "powershell": Path("WindowsPowerShell/v1.0/powershell.exe"),
            "diskpart": Path("diskpart.exe"),
        }[tool]
        expected = system / relative
        if configured is not None and (
            not Path(configured).is_absolute() or Path(configured) != expected
        ):
            raise NativeWindowsToolUnavailableError("tool override is not the native Windows path")
        for path in (expected, *expected.parents):
            info = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise NativeWindowsToolUnavailableError("redirected Windows tool path")
        if not expected.is_file() or expected.resolve(strict=True) != expected:
            raise NativeWindowsToolUnavailableError("native Windows tool identity mismatch")
        return str(expected)
    except (OSError, ValueError, KeyError) as exc:
        raise NativeWindowsToolUnavailableError("native Windows tool is unavailable") from exc


def native_tool_environment() -> dict[str, str]:
    """Keep inherited settings but exclude user-controlled tool/module searches."""
    windows, system = _checked_directories()
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PATH", "PSMODULEPATH", "SYSTEMROOT", "WINDIR"}}
    environment.update({
        "PATH": str(system) + os.pathsep + str(windows),
        "SystemRoot": str(windows), "WINDIR": str(windows),
        "PSModulePath": str(system / "WindowsPowerShell/v1.0/Modules"),
    })
    return environment
