from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from rekordbox_usb.windows_vhdx_nml_adapter import WindowsVhdxPathOutsideStorageError
from rekordbox_usb.source_write_guard import (
    SourceWriteBoundary, SourceWriteBoundaryError, WriteDenial, WriteIntent, WritePlan,
)
from rekordbox_usb.windows_virtual_disk_backend import (
    PowerShellOperationDiagnostic,
    PowerShellWindowsVirtualDiskBackend,
    SubprocessPowerShellRunner,
    WindowsStorageElevationRequiredError,
    WindowsVirtualDiskAmbiguousError,
    WindowsVirtualDiskIncompatibleError,
    WindowsVirtualDiskBackendError,
    WindowsVirtualDiskFormatError,
    _RESOLVE_IMAGE_DISK,
)


class FakeRunner:
    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.scripts: list[str] = []

    def run(self, script: str, timeout_seconds: float) -> str:
        self.scripts.append(script)
        return self.outputs.pop(0) if self.outputs else ""


@pytest.mark.skipif(sys.platform != "win32", reason="Real PowerShell over synthetic image/disk objects")
@pytest.mark.parametrize("image_number,device_number,disk_number,bus,confirmed,works", [
    (None, 7, 7, "File Backed Virtual", 7, True),
    (0, 0, 0, "File Backed Virtual", 0, True),
    (8, 7, 7, "File Backed Virtual", 7, False),
    (None, 7, 8, "File Backed Virtual", 7, False),
    (None, 7, 7, "SATA", 7, False),
    (None, 7, 7, "File Backed Virtual", 8, False),
    (None, None, 7, "File Backed Virtual", 7, False),
])
def test_exact_image_binding_with_null_number_and_ambiguous_synthetic_cases(
    image_number, device_number, disk_number, bus, confirmed, works,
):
    number = "$null" if image_number is None else str(image_number)
    device = rf"\\.\PhysicalDrive{device_number}" if device_number is not None else "unavailable"
    # Shadow cmdlets with authorial fixtures: no real storage inspection or effects.
    script = f"""
$ErrorActionPreference = 'Stop'
$p = 'C:\\synthetic.vhdx'
$image = [pscustomobject]@{{Number={number}; DevicePath='{device}'; Attached=$true}}
function Get-Disk {{ param($Number,$ErrorAction)
    if ($Number -ne {device_number if device_number is not None else 7}) {{ throw 'wrong query' }}
    [pscustomobject]@{{Number={disk_number};BusType='{bus}'}}
}}
function Get-DiskImage {{ param($ImagePath,$ErrorAction)
    if ($ImagePath -ne $p) {{ throw 'wrong image' }}
    [pscustomobject]@{{Attached=$true;DevicePath='\\\\.\\PhysicalDrive{confirmed}'}}
}}
""" + _RESOLVE_IMAGE_DISK.format() + "\n[Console]::Out.Write($disk[0].Number)"
    if works:
        assert SubprocessPowerShellRunner().run(script, 10).strip() == str(disk_number)
    else:
        with pytest.raises(WindowsVirtualDiskAmbiguousError):
            SubprocessPowerShellRunner().run(script, 10)


def backend(tmp_path: Path, *outputs: str) -> tuple[PowerShellWindowsVirtualDiskBackend, FakeRunner, Path]:
    root = tmp_path / "managed Unicode ç"
    root.mkdir(exist_ok=True)
    runner = FakeRunner(*outputs)
    value = PowerShellWindowsVirtualDiskBackend(root, runner)
    source = tmp_path / "synthetic-usb"
    source.mkdir(exist_ok=True)
    value.bind_write_boundary(SourceWriteBoundary(source))
    return value, runner, root / "catálogo test.vhdx"


class _Owned:
    consumed = True

    def __init__(self, action: str, path: Path) -> None:
        self.action = action
        self.path = path

    def assert_consumed(self, *, action: str, path: Path, current_identity) -> None:
        assert action == self.action and path == self.path


def allow(
    value: PowerShellWindowsVirtualDiskBackend,
    path: Path,
    action: str | None = None,
) -> None:
    value.set_write_permit(value._write_boundary.authorize(WritePlan(WriteIntent.VHDX, (path,))))
    if action is not None:
        value.set_ownership_capability(_Owned(action, path))  # type: ignore[arg-type]


def test_create_and_attach_quote_paths_with_spaces_and_unicode(tmp_path: Path) -> None:
    value, runner, path = backend(tmp_path)
    allow(value, path)
    value.create_vhdx(path, 64, 12.5)
    allow(value, path, "attach")
    value.attach(path, 12.5)
    assert "New-VHD" in runner.scripts[0] and str(path) in runner.scripts[0]
    assert "-Dynamic" in runner.scripts[0] and "Mount-VHD" in runner.scripts[1]
    assert all("clean" not in script.casefold() for script in runner.scripts)


def test_initialization_and_format_are_vhdx_path_discovered_only(tmp_path: Path) -> None:
    value, runner, path = backend(tmp_path)
    allow(value, path, "initialize")
    value.initialize_if_needed(path, 5)
    allow(value, path, "format")
    value.format_if_needed(path, "NTFS", "TRAKTORBOX_USB", 5)
    joined = "\n".join(runner.scripts)
    assert "Get-DiskImage -ImagePath" in joined
    assert "New-Partition" in joined and "-AssignDriveLetter" in joined and "Format-Volume" in joined
    assert "Format-Volume -Partition $partition" in joined and "-Force -ErrorAction Stop" in joined
    assert "Get-Volume -Partition $partition -ErrorAction Stop" in joined
    assert "Get-Volume -DriveLetter $partition.DriveLetter -ErrorAction Stop" in joined
    assert "Add-PartitionAccessPath -DiskNumber $disk[0].Number -PartitionNumber $partition.PartitionNumber -AssignDriveLetter -ErrorAction Stop" in joined
    assert "Initialize-Disk -Number $disk[0].Number -PartitionStyle GPT -ErrorAction Stop" in joined
    assert "Remove-Partition" not in joined and "Clear-Disk" not in joined


def test_existing_format_is_not_silently_reformatted(tmp_path: Path) -> None:
    value, _, path = backend(tmp_path)
    with pytest.raises(WindowsVirtualDiskIncompatibleError):
        value.format_if_needed(path, "FAT32", "x", 5)


def test_format_nonterminating_backend_failure_becomes_typed_error(tmp_path: Path) -> None:
    class FailingRunner(FakeRunner):
        def run(self, script: str, timeout_seconds: float) -> str:
            raise WindowsVirtualDiskBackendError("format command reported a non-terminating error")

    root = tmp_path / "managed"
    root.mkdir()
    path = root / "catalog.vhdx"
    value = PowerShellWindowsVirtualDiskBackend(root, FailingRunner())
    source = tmp_path / "synthetic-usb"
    source.mkdir()
    value.bind_write_boundary(SourceWriteBoundary(source))
    allow(value, path, "format")
    with pytest.raises(WindowsVirtualDiskFormatError, match="non-terminating"):
        value.format_if_needed(path, "NTFS", "TRAKTORBOX_USB", 5)


def test_inspection_handles_absent_and_attached_json(tmp_path: Path) -> None:
    value, _, path = backend(tmp_path, '{"exists":false}')
    assert not value.inspect(path).exists
    value, _, path = backend(tmp_path, '{"exists":true,"attached":true,"initialized":true,"filesystem":"NTFS","volume_label":"TRAKTORBOX_USB","mount_point":"T:\\\\","logical_size_mb":64}')
    state = value.inspect(path)
    assert state.attached and state.filesystem == "NTFS" and state.logical_size_mb == 64 and state.compatible
    assert state.volume_label == "TRAKTORBOX_USB" and state.mount_point == Path("T:\\")
    script = value._runner.scripts[0]
    assert "Get-Volume -DriveLetter ([char]$drive_letter) -ErrorAction Stop" in script
    assert "Get-CimInstance -Namespace 'root/Microsoft/Windows/Storage' -ClassName MSFT_Volume" in script
    assert "$drive_letter:" not in script
    assert '${drive_letter}:' in script
    assert "Get-CimInstance -ClassName Win32_Volume -Filter \"DriveLetter = '$drive_letter_with_colon'\"" in script
    assert "DriveLetter = '$drive_letter' OR DriveLetter = '$drive_letter_with_colon'" in script
    assert "function Normalize-DriveLetter" in script
    assert "TrimEnd('\\').TrimEnd(':')" in script
    assert "^[A-Za-z](:)?" not in script
    assert "filesystem = $filesystem; volume_label = $volume_label" in script
    assert "volume_source" in script
    assert (
        "$selected_volume = if ($partition_volume_candidates.Count -eq 1)"
        in script
    )


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell syntax validation requires Windows")
def test_inspection_script_parses_with_delimited_drive_letter(tmp_path: Path) -> None:
    value, runner, path = backend(tmp_path, '{"exists":false}')
    value.inspect(path)
    script = runner.scripts[0]
    assert "$drive_letter:" not in script
    assert '$drive_letter_with_colon = if ($null -ne $drive_letter) { "${drive_letter}:" }' in script
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    parser = (
        "$source = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('"
        + encoded
        + "')); $tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object Message; exit 1 }"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", parser],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell normalization validation requires Windows")
def test_drive_letter_normalization_accepts_only_one_letter(tmp_path: Path) -> None:
    value, runner, path = backend(tmp_path, '{"exists":false}')
    value.inspect(path)
    script = runner.scripts[0]
    function_start = script.index("function Normalize-DriveLetter")
    function_end = script.index("\n}\n", function_start) + 3
    normalizer = script[function_start:function_end]
    harness = """
$valid = @('E', 'E:', 'E:\\', 'e')
$actual = @($valid | ForEach-Object { Normalize-DriveLetter $_ })
if (($actual -join ',') -ne 'E,E,E,E') { exit 1 }
$invalid = @('', 'EE', 'E:\\pasta', '1')
$rejected = 0
foreach ($candidate in $invalid) {
    try { Normalize-DriveLetter $candidate | Out-Null } catch { $rejected++ }
}
if ($rejected -ne $invalid.Count) { exit 2 }
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", normalizer + harness],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_inspection_preserves_empty_volume_metadata_as_none(tmp_path: Path) -> None:
    payload = '{"exists":true,"attached":true,"initialized":true,"filesystem":"","volume_label":"","mount_point":null,"logical_size_mb":64}'
    value, _, path = backend(tmp_path, payload)
    state = value.inspect(path)
    assert state.filesystem is None and state.volume_label is None and state.mount_point is None


def test_inspection_exposes_read_only_volume_access_path_without_a_drive_letter(
    tmp_path: Path,
) -> None:
    access_path = "\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}\\"
    payload = json.dumps(
        {
            "exists": True,
            "attached": True,
            "initialized": True,
            "filesystem": "NTFS",
            "volume_label": "TRAKTORBOX_TEST",
            "mount_point": None,
            "access_path": access_path,
            "logical_size_mb": 64,
            "partition_present": True,
        }
    )
    value, _, path = backend(tmp_path, payload)

    state = value.inspect(path)

    assert state.mount_point is None
    assert state.access_path == Path(access_path)


def test_inspection_with_no_volume_is_not_compatible(tmp_path: Path) -> None:
    payload = '{"exists":true,"attached":true,"initialized":true,"filesystem":null,"volume_label":null,"mount_point":"E:\\\\","logical_size_mb":64,"partition_present":true}'
    value, _, path = backend(tmp_path, payload)
    state = value.inspect(path)
    assert state.mount_point == Path("E:\\")
    assert state.filesystem is None and state.volume_label is None and not state.compatible


def test_inspection_preserves_ambiguous_volume_as_typed_error(tmp_path: Path) -> None:
    class AmbiguousRunner(FakeRunner):
        def run(self, script: str, timeout_seconds: float) -> str:
            raise WindowsVirtualDiskAmbiguousError("managed VHDX has multiple volumes")

    root = tmp_path / "managed"
    root.mkdir()
    path = root / "catalog.vhdx"
    with pytest.raises(WindowsVirtualDiskAmbiguousError, match="multiple volumes"):
        PowerShellWindowsVirtualDiskBackend(root, AmbiguousRunner()).inspect(path)


def test_inspection_uses_cim_result_after_a_successful_format_result(tmp_path: Path) -> None:
    formatted = '{"FileSystem":"NTFS","FileSystemLabel":"TRAKTORBOX_TEST","DriveLetter":"E"}'
    inspected = '{"exists":true,"attached":true,"initialized":true,"filesystem":"NTFS","volume_label":"TRAKTORBOX_TEST","mount_point":"E:\\\\","logical_size_mb":64,"partition_present":true,"volume_found":true,"volume_source":"cim-msft-volume"}'
    value, _, path = backend(tmp_path, formatted, inspected)
    allow(value, path, "format")
    value.format_if_needed(path, "NTFS", "TRAKTORBOX_TEST", 5)
    state = value.inspect(path)
    assert state.filesystem == "NTFS" and state.volume_label == "TRAKTORBOX_TEST"
    assert state.mount_point == Path("E:\\") and state.compatible


@pytest.mark.parametrize("source", ["cim-msft-volume", "cim-win32-volume"])
def test_inspection_diagnostics_preserve_selected_cim_source(tmp_path: Path, source: str) -> None:
    payload = (
        '{"exists":true,"attached":true,"initialized":true,"filesystem":"NTFS",'
        '"volume_label":"TRAKTORBOX_TEST","mount_point":"E:\\\\","logical_size_mb":64,'
        f'"partition_present":true,"volume_found":true,"volume_source":"{source}"}}'
    )
    root = tmp_path / "managed"
    root.mkdir()
    diagnostics: list[PowerShellOperationDiagnostic] = []
    value = PowerShellWindowsVirtualDiskBackend(root, FakeRunner(payload), diagnostic_sink=diagnostics.append)
    state = value.inspect(root / "catalog.vhdx")
    assert state.compatible
    assert source in diagnostics[-1].stdout


def test_ambiguous_state_and_external_paths_are_rejected(tmp_path: Path) -> None:
    value, _, path = backend(tmp_path)
    with pytest.raises(WindowsVhdxPathOutsideStorageError):
        value.attach(tmp_path / "outside.vhdx", 5)

    class AmbiguousRunner(FakeRunner):
        def run(self, script: str, timeout_seconds: float) -> str:
            raise WindowsVirtualDiskAmbiguousError("AMBIGUOUS")

    root = tmp_path / "other"
    root.mkdir()
    value = PowerShellWindowsVirtualDiskBackend(root, AmbiguousRunner())
    value.bind_write_boundary(SourceWriteBoundary(tmp_path / "synthetic-usb"))
    allow(value, root / "safe.vhdx", "attach")
    with pytest.raises(WindowsVirtualDiskAmbiguousError):
        value.attach(root / "safe.vhdx", 5)


def test_detach_only_uses_managed_path(tmp_path: Path) -> None:
    value, runner, path = backend(tmp_path)
    allow(value, path, "detach")
    value.detach(path, 5)
    assert "Dismount-VHD" in runner.scripts[-1]
    with pytest.raises(WindowsVhdxPathOutsideStorageError):
        value.detach(tmp_path / "other.vhdx", 5)


def test_direct_backend_requires_exact_fresh_source_bound_permit(tmp_path: Path) -> None:
    value, runner, path = backend(tmp_path)
    with pytest.raises(SourceWriteBoundaryError) as missing:
        value.attach(path, 5)
    assert missing.value.code is WriteDenial.PERMIT_MISMATCH

    other = path.with_name("other.vhdx")
    allow(value, other)
    with pytest.raises(SourceWriteBoundaryError) as wrong_target:
        value.attach(path, 5)
    assert wrong_target.value.code is WriteDenial.TARGET_NOT_IN_WRITE_PLAN

    other_source = tmp_path / "different-usb"
    other_source.mkdir()
    value.set_write_permit(
        SourceWriteBoundary(other_source).authorize(WritePlan(WriteIntent.VHDX, (path,)))
    )
    with pytest.raises(SourceWriteBoundaryError) as wrong_source:
        value.attach(path, 5)
    assert wrong_source.value.code is WriteDenial.PERMIT_MISMATCH

    allow(value, path)
    (tmp_path / "synthetic-usb").rename(tmp_path / "removed-usb")
    with pytest.raises(SourceWriteBoundaryError) as stale:
        value.attach(path, 5)
    assert stale.value.code is WriteDenial.PATH_IDENTITY_CHANGED
    assert not runner.scripts


def test_subprocess_stderr_and_timeout_are_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rekordbox_usb.windows_virtual_disk_backend.sys.platform", "win32")

    def denied(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        return subprocess.CompletedProcess(args, 1, "", "Access is denied")

    monkeypatch.setattr("rekordbox_usb.windows_virtual_disk_backend.subprocess.run", denied)
    with pytest.raises(WindowsStorageElevationRequiredError):
        SubprocessPowerShellRunner().run("x", 1)

    def denied_localized(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 1, "", "Você não tem a permissão necessária para concluir esta tarefa"
        )

    monkeypatch.setattr(
        "rekordbox_usb.windows_virtual_disk_backend.subprocess.run", denied_localized
    )
    with pytest.raises(WindowsStorageElevationRequiredError):
        SubprocessPowerShellRunner().run("x", 1)

    def timed_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr("rekordbox_usb.windows_virtual_disk_backend.subprocess.run", timed_out)
    with pytest.raises(Exception, match="timed out"):
        SubprocessPowerShellRunner().run("x", 1)
