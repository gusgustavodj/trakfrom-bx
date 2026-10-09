"""Safe diskpart fallback for managed VHDX attach lifecycle.

Diskpart receives only a generated script that names the managed VHDX. Storage
inspection and mutation remain delegated to the PowerShell backend, which maps
the disk through the VHDX path instead of accepting a physical-disk number.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import re
from pathlib import Path
import subprocess
import sys
from typing import Protocol, runtime_checkable
from .windows_native_tools import (
    NativeWindowsToolUnavailableError, native_tool_environment, resolve_native_tool,
)
from .windows_secure_scripts import secure_diskpart_script, SecureScriptError

from .windows_vhdx_nml_adapter import WindowsVhdxPathOutsideStorageError, WindowsVhdxVolumeInspection
from .source_write_guard import (
    SourceWriteBoundary, SourceWriteBoundaryError, WriteDenial, WriteIntent,
    WritePermit, WritePlan,
)
from .windows_virtual_disk_backend import (
    PowerShellRunner,
    PowerShellWindowsVirtualDiskBackend,
    WindowsStorageElevationRequiredError,
    WindowsStorageToolUnavailableError,
    WindowsVirtualDiskAmbiguousError,
    WindowsVirtualDiskAttachError,
    WindowsVirtualDiskBackendError,
)
from .vhdx_ownership import (
    VhdxFileIdentity,
    VhdxOwnershipCapability,
    VhdxOwnershipDenial,
    VhdxOwnershipError,
)


class DiskpartExecutionError(WindowsVirtualDiskBackendError):
    """Raised when diskpart cannot complete a managed VHDX operation."""


class DiskpartToolUnavailableError(WindowsStorageToolUnavailableError):
    """Raised when diskpart.exe is unavailable on Windows."""


@runtime_checkable
class DiskpartRunner(Protocol):
    """Run one temporary diskpart script and remove it before returning."""

    def run(self, script: str, timeout_seconds: float) -> str: ...

    def is_available(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class SubprocessDiskpartRunner:
    """Single subprocess boundary for diskpart, with external transient scripts."""

    executable: str | None = None

    def is_available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            resolve_native_tool("diskpart", self.executable)
            return True
        except NativeWindowsToolUnavailableError:
            return False

    def run(
        self, script: str, timeout_seconds: float, *,
        boundary: SourceWriteBoundary | None = None,
        vhdx_path: Path | None = None,
        vhdx_permit: WritePermit | None = None,
    ) -> str:
        if boundary is None or vhdx_path is None or vhdx_permit is None:
            raise SourceWriteBoundaryError(WriteDenial.PERMIT_MISMATCH)
        vhdx_permit.require(boundary, vhdx_path, WriteIntent.VHDX)
        quoted = _diskpart_quote(vhdx_path)
        fixed_scripts = {
            f"select vdisk file={quoted}\nattach vdisk\n",
            f"select vdisk file={quoted}\ndetach vdisk\n",
        }
        if script not in fixed_scripts and re.fullmatch(
            rf"create vdisk file={re.escape(quoted)} maximum=[1-9][0-9]* type=expandable\n",
            script,
        ) is None:
            raise SourceWriteBoundaryError(WriteDenial.TARGET_NOT_IN_WRITE_PLAN)
        try:
            executable = resolve_native_tool("diskpart", self.executable)
            environment = native_tool_environment()
        except NativeWindowsToolUnavailableError as exc:
            raise DiskpartToolUnavailableError("trusted diskpart.exe is unavailable") from exc
        try:
            with secure_diskpart_script(script, boundary) as script_path:
                vhdx_permit.require(boundary, vhdx_path, WriteIntent.VHDX)
                completed = subprocess.run(
                    [executable, "/s", str(script_path)],
                    executable=executable,
                    env=environment,
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=timeout_seconds,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        except SecureScriptError as exc:
            raise DiskpartExecutionError("secure DiskPart script storage failed") from exc
        except FileNotFoundError as exc:
            raise DiskpartToolUnavailableError("diskpart.exe is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise DiskpartExecutionError("diskpart operation timed out") from exc
        except OSError as exc:
            if getattr(exc, "winerror", None) == 740:
                raise WindowsStorageElevationRequiredError(
                    "diskpart requires an elevated Windows process"
                ) from exc
            raise DiskpartExecutionError("diskpart could not be started") from exc
        if completed.returncode == 0:
            return completed.stdout
        message = completed.stderr.strip() or completed.stdout.strip() or "diskpart operation failed"
        lowered = message.casefold()
        if "access is denied" in lowered or "administrator" in lowered or "elevation" in lowered:
            raise WindowsStorageElevationRequiredError(message)
        raise DiskpartExecutionError(message)


class WindowsVirtualDiskBackendKind(StrEnum):
    HYPER_V = "hyper_v"
    DISKPART = "diskpart"


@dataclass(frozen=True, slots=True)
class WindowsVirtualDiskCapabilities:
    hyper_v_available: bool
    diskpart_available: bool


@dataclass(frozen=True, slots=True)
class SelectedWindowsVirtualDiskBackend:
    kind: WindowsVirtualDiskBackendKind
    backend: object


def _diskpart_quote(path: Path) -> str:
    value = str(path)
    if '"' in value or "\r" in value or "\n" in value:
        raise WindowsVhdxPathOutsideStorageError("managed VHDX path cannot be represented safely by diskpart")
    return f'"{value}"'


class DiskpartVirtualDiskBackend:
    """Managed VHDX backend using diskpart only for create, attach, and detach."""

    def __init__(
        self,
        storage_root: Path,
        *,
        diskpart_runner: DiskpartRunner | None = None,
        storage_backend: PowerShellWindowsVirtualDiskBackend | None = None,
    ) -> None:
        if not storage_root.is_absolute() or not storage_root.exists() or not storage_root.is_dir() or storage_root.is_symlink():
            raise WindowsVhdxPathOutsideStorageError("storage_root must be an existing non-symlink directory")
        self._storage = storage_backend or PowerShellWindowsVirtualDiskBackend(storage_root)
        self._root = storage_root
        self._diskpart = diskpart_runner or SubprocessDiskpartRunner()
        self._write_boundary: SourceWriteBoundary | None = None
        self._write_permit: WritePermit | None = None
        self._ownership_capability: VhdxOwnershipCapability | None = None

    def bind_write_boundary(self, boundary: SourceWriteBoundary) -> None:
        self._write_boundary = boundary
        self._write_permit = None
        if hasattr(self._storage, "bind_write_boundary"):
            self._storage.bind_write_boundary(boundary)

    def set_write_permit(self, permit: WritePermit) -> None:
        self._write_permit = permit

    def set_ownership_capability(self, capability: VhdxOwnershipCapability) -> None:
        self._ownership_capability = capability

    def _consume_ownership_capability(
        self, path: Path, action: str, *, delegate: bool = False
    ) -> None:
        capability = self._ownership_capability
        self._ownership_capability = None
        if capability is None:
            raise VhdxOwnershipError(VhdxOwnershipDenial.OWNERSHIP_REQUIRED)
        if delegate:
            if hasattr(self._storage, "set_ownership_capability"):
                self._storage.set_ownership_capability(capability)
                return
            raise VhdxOwnershipError(VhdxOwnershipDenial.OWNERSHIP_REQUIRED)
        capability.assert_consumed(
            action=action,
            path=path,
            current_identity=VhdxFileIdentity.observe(path),
        )

    def _consume_write_permit(self, path: Path, *, delegate: bool = False) -> WritePermit:
        permit, boundary = self._write_permit, self._write_boundary
        self._write_permit = None
        if permit is None or boundary is None:
            raise SourceWriteBoundaryError(WriteDenial.PERMIT_MISMATCH)
        permit.require(boundary, path, WriteIntent.VHDX)
        if delegate:
            if hasattr(self._storage, "set_write_permit"):
                self._storage.set_write_permit(permit)
            else:
                permit.close()
        return permit

    def create_vhdx(self, path: Path, logical_size_mb: int, timeout_seconds: float) -> None:
        self._path(path)
        permit = self._consume_write_permit(path)
        if logical_size_mb <= 0:
            raise DiskpartExecutionError("logical_size_mb must be positive")
        if path.exists():
            raise DiskpartExecutionError("managed VHDX already exists")
        script = f"create vdisk file={_diskpart_quote(path)} maximum={logical_size_mb} type=expandable\n"
        try:
            self._run(script, timeout_seconds, WindowsVirtualDiskAttachError, path, permit)
        finally:
            permit.close()

    def attach(self, path: Path, timeout_seconds: float) -> None:
        self._path(path)
        permit = self._consume_write_permit(path)
        self._consume_ownership_capability(path, "attach")
        script = f"select vdisk file={_diskpart_quote(path)}\nattach vdisk\n"
        try:
            self._run(script, timeout_seconds, WindowsVirtualDiskAttachError, path, permit)
        finally:
            permit.close()
        if not self.inspect(path).attached:
            raise WindowsVirtualDiskAttachError("diskpart did not attach managed VHDX")

    def inspect(self, path: Path) -> WindowsVhdxVolumeInspection:
        self._path(path)
        return self._storage.inspect(path)

    def initialize_if_needed(self, path: Path, timeout_seconds: float) -> None:
        self._path(path)
        self._consume_write_permit(path, delegate=True)
        self._consume_ownership_capability(path, "initialize", delegate=True)
        self._storage.initialize_if_needed(path, timeout_seconds)

    def format_if_needed(self, path: Path, filesystem: str, label: str, timeout_seconds: float) -> None:
        self._path(path)
        self._consume_write_permit(path, delegate=True)
        self._consume_ownership_capability(path, "format", delegate=True)
        self._storage.format_if_needed(path, filesystem, label, timeout_seconds)

    def assign_mount(self, path: Path, preferred_drive_letter: str | None, timeout_seconds: float) -> Path:
        self._path(path)
        self._consume_write_permit(path, delegate=True)
        self._consume_ownership_capability(path, "assign_mount", delegate=True)
        return self._storage.assign_mount(path, preferred_drive_letter, timeout_seconds)

    def detach(self, path: Path, timeout_seconds: float) -> None:
        self._path(path)
        permit = self._consume_write_permit(path)
        self._consume_ownership_capability(path, "detach")
        script = f"select vdisk file={_diskpart_quote(path)}\ndetach vdisk\n"
        try:
            self._run(script, timeout_seconds, WindowsVirtualDiskAttachError, path, permit)
        finally:
            permit.close()

    def remove_vhdx(self, path: Path) -> None:
        self._path(path)
        self._consume_write_permit(path, delegate=True)
        self._consume_ownership_capability(path, "remove_vhdx", delegate=True)
        self._storage.remove_vhdx(path)

    def _path(self, path: Path) -> None:
        if not path.is_absolute() or path.suffix.lower() != ".vhdx":
            raise WindowsVhdxPathOutsideStorageError("managed disk must be an absolute .vhdx path")
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise WindowsVhdxPathOutsideStorageError(str(path)) from exc

    def _run(
        self, script: str, timeout_seconds: float,
        error_type: type[WindowsVirtualDiskBackendError], path: Path, permit: WritePermit,
    ) -> None:
        try:
            if isinstance(self._diskpart, SubprocessDiskpartRunner):
                self._diskpart.run(
                    script, timeout_seconds, boundary=self._write_boundary,
                    vhdx_path=path, vhdx_permit=permit,
                )
            else:
                self._diskpart.run(script, timeout_seconds)
        except (DiskpartToolUnavailableError, WindowsStorageElevationRequiredError, WindowsVirtualDiskAmbiguousError):
            raise
        except WindowsVirtualDiskBackendError as exc:
            raise error_type(str(exc)) from exc


def detect_windows_virtual_disk_capabilities(
    power_shell_runner: PowerShellRunner,
    diskpart_runner: DiskpartRunner,
) -> WindowsVirtualDiskCapabilities:
    """Detect choices before lifecycle work; no fallback occurs after an action."""
    try:
        result = power_shell_runner.run(
            "@('New-VHD','Mount-VHD','Dismount-VHD') | ForEach-Object { [bool](Get-Command $_ -ErrorAction SilentlyContinue) } | ConvertTo-Json -Compress",
            10.0,
        )
        values = json.loads(result)
        hyper_v_available = isinstance(values, list) and values == [True, True, True]
    except WindowsVirtualDiskBackendError:
        hyper_v_available = False
    return WindowsVirtualDiskCapabilities(hyper_v_available, diskpart_runner.is_available())


def select_windows_virtual_disk_backend(
    storage_root: Path,
    *,
    power_shell_runner: PowerShellRunner,
    diskpart_runner: DiskpartRunner,
    preference: WindowsVirtualDiskBackendKind | None = None,
) -> SelectedWindowsVirtualDiskBackend:
    """Select once, before publish: Hyper-V first unless an explicit fallback is chosen."""
    capabilities = detect_windows_virtual_disk_capabilities(power_shell_runner, diskpart_runner)
    selected = preference or (
        WindowsVirtualDiskBackendKind.HYPER_V if capabilities.hyper_v_available else WindowsVirtualDiskBackendKind.DISKPART
    )
    if selected is WindowsVirtualDiskBackendKind.HYPER_V:
        if not capabilities.hyper_v_available:
            raise WindowsStorageToolUnavailableError("Hyper-V VHD cmdlets are unavailable")
        return SelectedWindowsVirtualDiskBackend(
            selected, PowerShellWindowsVirtualDiskBackend(storage_root, power_shell_runner)
        )
    if not capabilities.diskpart_available:
        raise DiskpartToolUnavailableError("diskpart.exe is unavailable")
    return SelectedWindowsVirtualDiskBackend(
        selected,
        DiskpartVirtualDiskBackend(
            storage_root,
            diskpart_runner=diskpart_runner,
            storage_backend=PowerShellWindowsVirtualDiskBackend(storage_root, power_shell_runner),
        ),
    )
