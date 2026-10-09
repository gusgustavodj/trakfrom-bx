"""Native Windows PowerShell backend for one explicitly managed VHDX.

Every storage cmdlet discovers its disk through ``Get-DiskImage -ImagePath``.
The public API never accepts a disk number, and this module intentionally has
no command for wiping disks or removing partitions.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Protocol, runtime_checkable
from typing import Callable
from .windows_native_tools import (
    NativeWindowsToolUnavailableError, native_tool_environment, resolve_native_tool,
)

from .source_write_guard import (
    SourceWriteBoundary, SourceWriteBoundaryError, WriteDenial, WriteIntent,
    WritePermit,
)

from .windows_vhdx_nml_adapter import (
    WindowsVhdxPathOutsideStorageError,
    WindowsVhdxVolumeInspection,
)
from .vhdx_ownership import (
    VhdxFileIdentity,
    VhdxOwnershipCapability,
    VhdxOwnershipDenial,
    VhdxOwnershipError,
)


class WindowsVirtualDiskBackendError(RuntimeError):
    """Base error raised by the concrete native Windows backend."""


class WindowsStorageToolUnavailableError(WindowsVirtualDiskBackendError):
    """Raised when native PowerShell storage cmdlets are unavailable."""


class WindowsStorageElevationRequiredError(WindowsVirtualDiskBackendError):
    """Raised when Windows rejects a virtual-disk operation without elevation."""


class WindowsVirtualDiskAmbiguousError(WindowsVirtualDiskBackendError):
    """Raised when a VHDX cannot be mapped to exactly one virtual disk/volume."""


class WindowsVirtualDiskAttachError(WindowsVirtualDiskBackendError):
    """Raised when a managed VHDX cannot be attached."""


class WindowsVirtualDiskInitializeError(WindowsVirtualDiskBackendError):
    """Raised when a blank managed VHDX cannot be initialized safely."""


class WindowsVirtualDiskPartitionError(WindowsVirtualDiskBackendError):
    """Raised when one required partition cannot be created safely."""


class WindowsVirtualDiskFormatError(WindowsVirtualDiskBackendError):
    """Raised when a blank managed VHDX cannot be formatted safely."""


class WindowsVirtualDiskMountError(WindowsVirtualDiskBackendError):
    """Raised when a managed VHDX cannot receive or report a mount point."""


class WindowsVirtualDiskInspectError(WindowsVirtualDiskBackendError):
    """Raised when VHDX state cannot be inspected without ambiguity."""


class WindowsVirtualDiskDetachError(WindowsVirtualDiskBackendError):
    """Raised when a managed VHDX cannot be detached."""


class WindowsVirtualDiskIncompatibleError(WindowsVirtualDiskBackendError):
    """Raised when an existing VHDX has an unsafe filesystem or label state."""


@dataclass(frozen=True, slots=True)
class PowerShellOperationDiagnostic:
    """Safe record of one managed-VHDX PowerShell operation."""

    operation: str
    script: str
    stdout: str


@runtime_checkable
class PowerShellRunner(Protocol):
    """Run one encoded PowerShell command and return its standard output."""

    def run(self, script: str, timeout_seconds: float) -> str: ...


@dataclass(frozen=True, slots=True)
class SubprocessPowerShellRunner:
    """Small subprocess boundary; commands are encoded, never written to disk."""

    executable: str | None = None

    def run(self, script: str, timeout_seconds: float) -> str:
        if sys.platform != "win32":
            raise WindowsStorageToolUnavailableError("native Windows storage cmdlets require Windows")
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        try:
            executable = resolve_native_tool("powershell", self.executable)
            completed = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                executable=executable,
                env=native_tool_environment(),
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout_seconds,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (FileNotFoundError, NativeWindowsToolUnavailableError) as exc:
            raise WindowsStorageToolUnavailableError("PowerShell is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise WindowsVirtualDiskBackendError("PowerShell operation timed out") from exc
        if completed.returncode == 0:
            return completed.stdout
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        message = f"stdout={stdout!r}; stderr={stderr!r}" if stdout or stderr else "PowerShell storage operation failed"
        lowered = message.casefold()
        permission_denied = (
            "access is denied" in lowered
            or "administrator" in lowered
            or "elevation" in lowered
            or "acesso negado" in lowered
            or "permissão necessária" in lowered
            or "permissao necessaria" in lowered
        )
        if permission_denied:
            raise WindowsStorageElevationRequiredError(message)
        if "ambiguous" in lowered:
            raise WindowsVirtualDiskAmbiguousError(message)
        if "not recognized" in lowered or "not found" in lowered:
            raise WindowsStorageToolUnavailableError(message)
        raise WindowsVirtualDiskBackendError(message)


def _ps_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _within(path: Path, root: Path) -> None:
    if not path.is_absolute() or not root.is_absolute():
        raise WindowsVhdxPathOutsideStorageError("managed VHDX paths must be absolute")
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WindowsVhdxPathOutsideStorageError(str(path)) from exc


def _nonempty_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


_RESOLVE_IMAGE_DISK = r"""
# Some Windows providers return a null MSFT_DiskImage.Number. Derive it only
# from this exact image's DevicePath, never from a caller-supplied disk number.
$image_device = [string]$image.DevicePath
if ($image_device -notmatch '^\\\\\.\\PhysicalDrive([0-9]+)$') {{
    throw 'AMBIGUOUS: image has no exact physical device path'
}}
$device_number = [uint32]$Matches[1]
if ($null -ne $image.Number -and $image.Number -ne $device_number) {{
    throw 'AMBIGUOUS: image number and device path disagree'
}}
$disk = @(Get-Disk -Number $device_number -ErrorAction Stop)
if ($disk.Count -ne 1 -or $disk[0].Number -ne $device_number -or
    $disk[0].BusType -ne 'File Backed Virtual') {{
    throw 'AMBIGUOUS: VHDX disk identity is not unique'
}}
$confirmed_image = Get-DiskImage -ImagePath $p -ErrorAction Stop
if (-not $confirmed_image.Attached -or $confirmed_image.DevicePath -ne $image_device) {{
    throw 'AMBIGUOUS: image binding changed during inspection'
}}
"""


_DISK_FROM_IMAGE = """
$ErrorActionPreference = 'Stop'
$p = {path}
$image = Get-DiskImage -ImagePath $p -ErrorAction Stop
if (-not $image.Attached) {{ throw 'managed VHDX is not attached' }}
""" + _RESOLVE_IMAGE_DISK


class PowerShellWindowsVirtualDiskBackend:
    """Native managed-VHDX implementation using only PowerShell Storage cmdlets."""

    def __init__(
        self,
        storage_root: Path,
        runner: PowerShellRunner | None = None,
        *,
        expected_filesystem: str = "NTFS",
        expected_volume_label: str | None = None,
        diagnostic_sink: Callable[[PowerShellOperationDiagnostic], None] | None = None,
    ) -> None:
        if not storage_root.is_absolute() or not storage_root.exists() or not storage_root.is_dir() or storage_root.is_symlink():
            raise WindowsVhdxPathOutsideStorageError("storage_root must be an existing non-symlink directory")
        self._storage_root = storage_root
        self._runner = runner or SubprocessPowerShellRunner()
        self._expected_filesystem = expected_filesystem
        self._expected_volume_label = expected_volume_label
        self._diagnostic_sink = diagnostic_sink
        self._write_boundary: SourceWriteBoundary | None = None
        self._write_permit: WritePermit | None = None
        self._ownership_capability: VhdxOwnershipCapability | None = None

    def bind_write_boundary(self, boundary: SourceWriteBoundary) -> None:
        self._write_boundary = boundary
        self._write_permit = None

    def set_write_permit(self, permit: WritePermit) -> None:
        self._write_permit = permit

    def set_ownership_capability(self, capability: VhdxOwnershipCapability) -> None:
        self._ownership_capability = capability

    def _consume_ownership_capability(self, path: Path, action: str) -> None:
        capability = self._ownership_capability
        self._ownership_capability = None
        if capability is None:
            raise VhdxOwnershipError(VhdxOwnershipDenial.OWNERSHIP_REQUIRED)
        capability.assert_consumed(
            action=action,
            path=path,
            current_identity=VhdxFileIdentity.observe(path),
        )

    def _consume_write_permit(self, path: Path) -> None:
        permit, boundary = self._write_permit, self._write_boundary
        self._write_permit = None
        if permit is None or boundary is None:
            raise SourceWriteBoundaryError(WriteDenial.PERMIT_MISMATCH)
        try:
            permit.require(boundary, path, WriteIntent.VHDX)
        finally:
            permit.close()

    def create_vhdx(self, path: Path, logical_size_mb: int, timeout_seconds: float) -> None:
        self._path(path)
        if logical_size_mb <= 0:
            raise WindowsVirtualDiskIncompatibleError("logical_size_mb must be positive")
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$p = {_ps_literal(path)}; "
            "if (Test-Path -LiteralPath $p) { throw 'managed VHDX already exists' }; "
            f"New-VHD -Path $p -Dynamic -SizeBytes ({logical_size_mb}MB) | Out-Null"
        )
        self._consume_write_permit(path)
        self._operation(script, timeout_seconds, WindowsVirtualDiskAttachError, "create_vhdx")

    def attach(self, path: Path, timeout_seconds: float) -> None:
        self._path(path)
        self._consume_write_permit(path)
        self._consume_ownership_capability(path, "attach")
        self._operation(
            "$ErrorActionPreference = 'Stop'; "
            f"Mount-VHD -Path {_ps_literal(path)} -NoDriveLetter | Out-Null",
            timeout_seconds,
            WindowsVirtualDiskAttachError,
            "attach",
        )

    def initialize_if_needed(self, path: Path, timeout_seconds: float) -> None:
        self._path(path)
        script = _DISK_FROM_IMAGE.format(path=_ps_literal(path)) + """
$parts = @($disk[0] | Get-Partition -ErrorAction SilentlyContinue)
if ($disk[0].PartitionStyle -eq 'RAW') {
    if ($parts.Count -ne 0) { throw 'AMBIGUOUS: RAW disk already has partitions' }
    Initialize-Disk -Number $disk[0].Number -PartitionStyle GPT -ErrorAction Stop | Out-Null
    $parts = @()
} elseif ($parts.Count -eq 0) {
    throw 'managed VHDX is not RAW but has no partition'
}
if ($parts.Count -eq 0) {
    $partition = New-Partition -DiskNumber $disk[0].Number -UseMaximumSize -AssignDriveLetter -ErrorAction Stop
    $parts = @($partition)
}
if ($parts.Count -ne 1) { throw 'AMBIGUOUS: managed VHDX requires exactly one partition' }
"""
        self._consume_write_permit(path)
        self._consume_ownership_capability(path, "initialize")
        self._operation(script, timeout_seconds, WindowsVirtualDiskInitializeError, "initialize_if_needed")

    def format_if_needed(self, path: Path, filesystem: str, label: str, timeout_seconds: float) -> None:
        self._path(path)
        if filesystem != "NTFS" or not label:
            raise WindowsVirtualDiskIncompatibleError("only explicit NTFS formatting is supported")
        script = _DISK_FROM_IMAGE.format(path=_ps_literal(path)) + f"""
$parts = @($disk[0] | Get-Partition -ErrorAction Stop)
if ($parts.Count -ne 1) {{ throw 'AMBIGUOUS: managed VHDX requires exactly one partition' }}
$partition = $parts[0]
if (-not $partition.DriveLetter) {{
    Add-PartitionAccessPath -DiskNumber $disk[0].Number -PartitionNumber $partition.PartitionNumber -AssignDriveLetter -ErrorAction Stop
    $partition = Get-Partition -DiskNumber $disk[0].Number -PartitionNumber $partition.PartitionNumber -ErrorAction Stop
}}
$volume = @(
    if ($partition.DriveLetter) {{
        Get-Volume -DriveLetter $partition.DriveLetter -ErrorAction Stop
    }} else {{
        Get-Volume -Partition $partition -ErrorAction SilentlyContinue
    }}
)
if ($volume.Count -gt 0 -and $volume[0].FileSystem) {{ throw 'managed VHDX already has a filesystem' }}
$format_result = Format-Volume -Partition $partition -FileSystem NTFS -NewFileSystemLabel {_ps_literal(label)} -Confirm:$false -Force -ErrorAction Stop
$formatted = @(Get-Volume -Partition $partition -ErrorAction Stop)
if ($formatted.Count -ne 1 -or $formatted[0].FileSystem -ne 'NTFS' -or $formatted[0].FileSystemLabel -ne {_ps_literal(label)} -or -not $partition.DriveLetter) {{
    throw 'managed VHDX format did not yield NTFS, the requested label, and a drive letter'
}}
$format_result | Select-Object FileSystem,FileSystemLabel,DriveLetter,HealthStatus,OperationalStatus | ConvertTo-Json -Compress
"""
        self._consume_write_permit(path)
        self._consume_ownership_capability(path, "format")
        self._operation(script, timeout_seconds, WindowsVirtualDiskFormatError, "format_if_needed")

    def assign_mount(self, path: Path, preferred_drive_letter: str | None, timeout_seconds: float) -> Path:
        self._path(path)
        if preferred_drive_letter is not None and (len(preferred_drive_letter) != 1 or not preferred_drive_letter.isalpha()):
            raise WindowsVirtualDiskMountError("preferred drive letter must be one letter")
        preferred = preferred_drive_letter.upper() if preferred_drive_letter else ""
        script = _DISK_FROM_IMAGE.format(path=_ps_literal(path)) + f"""
$parts = @($disk[0] | Get-Partition -ErrorAction Stop)
if ($parts.Count -ne 1) {{ throw 'AMBIGUOUS: managed VHDX requires exactly one partition' }}
$partition = $parts[0]
if ($partition.DriveLetter) {{ [Console]::Out.Write(($partition.DriveLetter.ToString() + ':\\')); exit 0 }}
$preferred = {_ps_literal(preferred)}
if ($preferred) {{
    if (Get-Volume -DriveLetter $preferred -ErrorAction SilentlyContinue) {{ throw 'requested drive letter is already in use' }}
    Set-Partition -DiskNumber $disk[0].Number -PartitionNumber $partition.PartitionNumber -NewDriveLetter $preferred -ErrorAction Stop
}} else {{
    Add-PartitionAccessPath -DiskNumber $disk[0].Number -PartitionNumber $partition.PartitionNumber -AssignDriveLetter -ErrorAction Stop
}}
$letter = (Get-Partition -DiskNumber $disk[0].Number -PartitionNumber $partition.PartitionNumber -ErrorAction Stop).DriveLetter
if (-not $letter) {{ throw 'managed VHDX did not receive a drive letter' }}
[Console]::Out.Write(($letter.ToString() + ':\\'))
"""
        self._consume_write_permit(path)
        self._consume_ownership_capability(path, "assign_mount")
        output = self._operation(script, timeout_seconds, WindowsVirtualDiskMountError, "assign_mount").strip()
        mount = Path(output)
        if not output or not mount.is_absolute():
            raise WindowsVirtualDiskMountError("PowerShell did not return an absolute mount point")
        return mount

    def inspect(self, path: Path) -> WindowsVhdxVolumeInspection:
        self._path(path)
        script = f"""
$ErrorActionPreference = 'Stop'
$p = {_ps_literal(path)}
if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {{
    [Console]::Out.Write('{{"exists":false}}'); exit 0
}}
$image = Get-DiskImage -ImagePath $p -ErrorAction Stop
if (-not $image.Attached) {{
    [Console]::Out.Write('{{"exists":true,"attached":false}}'); exit 0
}}
{_RESOLVE_IMAGE_DISK.format()}
$parts = @($disk[0] | Get-Partition -ErrorAction SilentlyContinue)
if ($parts.Count -gt 1) {{ throw 'AMBIGUOUS: managed VHDX has multiple partitions' }}
$partition = if ($parts.Count -eq 1) {{ $parts[0] }} else {{ $null }}
function Normalize-DriveLetter {{
    param([object]$Value)
    $normalized_letter = ([string]$Value).Trim().TrimEnd('\\').TrimEnd(':')
    if ($normalized_letter.Length -ne 1 -or $normalized_letter -notmatch '^[A-Za-z]$') {{ throw 'invalid drive letter' }}
    return $normalized_letter.ToUpperInvariant()
}}
$drive_letter = if ($null -ne $partition -and $partition.DriveLetter) {{ Normalize-DriveLetter $partition.DriveLetter }} else {{ $null }}
$drive_letter_with_colon = if ($null -ne $drive_letter) {{ "${{drive_letter}}:" }} else {{ $null }}
function Get-VolumeCandidate {{
    param([string]$Source, [object[]]$Items, [string]$LabelProperty, [string]$ExpectedDriveLetter)
    $matches = @(
        foreach ($item in @($Items)) {{
            $item_letter = Normalize-DriveLetter $item.DriveLetter
            if ($item_letter -eq $ExpectedDriveLetter) {{
                $filesystem = [string]$item.FileSystem
                $label = [string]$item.PSObject.Properties[$LabelProperty].Value
                [pscustomobject]@{{
                    source = $Source
                    drive_letter = $item_letter
                    filesystem = if ([string]::IsNullOrWhiteSpace($filesystem)) {{ $null }} else {{ $filesystem }}
                    volume_label = if ([string]::IsNullOrWhiteSpace($label)) {{ $null }} else {{ $label }}
                    access_path = if ([string]::IsNullOrWhiteSpace([string]$item.Path)) {{ $null }} else {{ [string]$item.Path }}
                }}
            }}
        }}
    )
    if ($matches.Count -gt 1) {{ throw "AMBIGUOUS: $Source returned multiple volumes for drive letter $ExpectedDriveLetter" }}
    return $matches
}}
$get_volume_items = @()
$partition_volume_items = @()
$msft_volume_items = @()
$win32_volume_items = @()
if ($null -ne $partition) {{
    try {{ $partition_volume_items = @(Get-Volume -Partition $partition -ErrorAction Stop) }} catch {{ $partition_volume_items = @() }}
}}
if ($null -ne $drive_letter) {{
    try {{ $get_volume_items = @(Get-Volume -DriveLetter ([char]$drive_letter) -ErrorAction Stop) }} catch {{ $get_volume_items = @() }}
    try {{ $msft_volume_items = @(Get-CimInstance -Namespace 'root/Microsoft/Windows/Storage' -ClassName MSFT_Volume -Filter "DriveLetter = '$drive_letter' OR DriveLetter = '$drive_letter_with_colon'" -ErrorAction Stop) }} catch {{ $msft_volume_items = @() }}
    try {{ $win32_volume_items = @(Get-CimInstance -ClassName Win32_Volume -Filter "DriveLetter = '$drive_letter_with_colon'" -ErrorAction Stop) }} catch {{ $win32_volume_items = @() }}
}}
$partition_volume_candidates = @(
    foreach ($item in $partition_volume_items) {{
        $filesystem = [string]$item.FileSystem
        $label = [string]$item.FileSystemLabel
        $access_path = [string]$item.Path
        if ([string]::IsNullOrWhiteSpace($access_path)) {{ $access_path = [string]$item.UniqueId }}
        [pscustomobject]@{{
            source = 'get-volume-partition'
            drive_letter = if ($item.DriveLetter) {{ Normalize-DriveLetter $item.DriveLetter }} else {{ $null }}
            filesystem = if ([string]::IsNullOrWhiteSpace($filesystem)) {{ $null }} else {{ $filesystem }}
            volume_label = if ([string]::IsNullOrWhiteSpace($label)) {{ $null }} else {{ $label }}
            access_path = if ([string]::IsNullOrWhiteSpace($access_path)) {{ $null }} else {{ $access_path }}
        }}
    }}
)
if ($partition_volume_candidates.Count -gt 1) {{ throw 'AMBIGUOUS: partition maps to multiple volumes' }}
$get_volume_candidates = @(Get-VolumeCandidate 'get-volume' $get_volume_items 'FileSystemLabel' $drive_letter)
$msft_volume_candidates = @(Get-VolumeCandidate 'cim-msft-volume' $msft_volume_items 'FileSystemLabel' $drive_letter)
$win32_volume_candidates = @(Get-VolumeCandidate 'cim-win32-volume' $win32_volume_items 'Label' $drive_letter)
$volume_candidates = @($partition_volume_candidates) + @($get_volume_candidates) + @($msft_volume_candidates) + @($win32_volume_candidates)
if ($volume_candidates.Count -gt 1) {{
    $metadata_values = @($volume_candidates | ForEach-Object {{ "$( $_.filesystem )|$( $_.volume_label )" }} | Select-Object -Unique)
    if ($metadata_values.Count -ne 1) {{
        $details = @($volume_candidates | ForEach-Object {{ "$( $_.source ):$( $_.filesystem ):$( $_.volume_label )" }}) -join '; '
        throw "AMBIGUOUS: VHDX volume metadata sources disagree: $details"
    }}
}}
$selected_volume = if ($partition_volume_candidates.Count -eq 1) {{ $partition_volume_candidates[0] }} elseif ($get_volume_candidates.Count -eq 1) {{ $get_volume_candidates[0] }} elseif ($msft_volume_candidates.Count -eq 1) {{ $msft_volume_candidates[0] }} elseif ($win32_volume_candidates.Count -eq 1) {{ $win32_volume_candidates[0] }} else {{ $null }}
$volume_found = ($null -ne $selected_volume)
$filesystem = if ($volume_found) {{ $selected_volume.filesystem }} else {{ $null }}
$volume_label = if ($volume_found) {{ $selected_volume.volume_label }} else {{ $null }}
[pscustomobject]@{{
    exists = $true; attached = $true; initialized = ($disk[0].PartitionStyle -ne 'RAW')
    filesystem = $filesystem; volume_label = $volume_label
    mount_point = if ($null -ne $drive_letter) {{ $drive_letter + ':\\' }} else {{ $null }}
    logical_size_mb = [math]::Floor($disk[0].Size / 1MB)
    partition_present = ($parts.Count -eq 1)
    volume_found = $volume_found
    volume_source = if ($volume_found) {{ $selected_volume.source }} else {{ $null }}
    access_path = if ($volume_found) {{ $selected_volume.access_path }} else {{ $null }}
    drive_letter = $drive_letter
}} | ConvertTo-Json -Compress
"""
        output = self._operation(script, 30.0, WindowsVirtualDiskInspectError, "inspect")
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise WindowsVirtualDiskInspectError("PowerShell inspection did not return JSON") from exc
        if not isinstance(value, dict):
            raise WindowsVirtualDiskInspectError("PowerShell inspection JSON must be an object")
        exists = value.get("exists") is True
        attached = value.get("attached") is True
        mount_text = value.get("mount_point")
        mount = Path(mount_text) if isinstance(mount_text, str) else None
        access_text = value.get("access_path")
        access_path = Path(access_text) if isinstance(access_text, str) else None
        filesystem = _nonempty_text(value.get("filesystem"))
        volume_label = _nonempty_text(value.get("volume_label"))
        compatible = filesystem == self._expected_filesystem and (
            self._expected_volume_label is None or volume_label == self._expected_volume_label
        )
        return WindowsVhdxVolumeInspection(
            path, exists, attached, value.get("initialized") is True,
            filesystem,
            volume_label,
            mount,
            value.get("logical_size_mb") if isinstance(value.get("logical_size_mb"), int) else None,
            compatible,
            value.get("partition_present") is True,
            access_path,
            VhdxFileIdentity.observe(path) if exists else None,
        )

    def detach(self, path: Path, timeout_seconds: float) -> None:
        self._path(path)
        self._consume_write_permit(path)
        self._consume_ownership_capability(path, "detach")
        self._operation(
            "$ErrorActionPreference = 'Stop'; "
            f"Dismount-VHD -Path {_ps_literal(path)} -ErrorAction Stop",
            timeout_seconds,
            WindowsVirtualDiskDetachError,
            "detach",
        )

    def remove_vhdx(self, path: Path) -> None:
        self._path(path)
        self._consume_write_permit(path)
        self._consume_ownership_capability(path, "remove_vhdx")
        path.unlink()

    def _path(self, path: Path) -> None:
        if path.suffix.lower() != ".vhdx":
            raise WindowsVhdxPathOutsideStorageError("managed disk must have .vhdx extension")
        _within(path, self._storage_root)

    def _operation(
        self,
        script: str,
        timeout_seconds: float,
        error_type: type[WindowsVirtualDiskBackendError],
        operation: str,
    ) -> str:
        if self._diagnostic_sink is not None:
            self._diagnostic_sink(PowerShellOperationDiagnostic(operation, script, ""))
        try:
            stdout = self._runner.run(script, timeout_seconds)
        except WindowsVirtualDiskBackendError as exc:
            if isinstance(exc, (WindowsStorageToolUnavailableError, WindowsStorageElevationRequiredError, WindowsVirtualDiskAmbiguousError)):
                raise
            raise error_type(str(exc)) from exc
        except Exception as exc:
            raise error_type("native Windows virtual disk operation failed") from exc
        if self._diagnostic_sink is not None:
            self._diagnostic_sink(PowerShellOperationDiagnostic(operation, script, stdout))
        return stdout
