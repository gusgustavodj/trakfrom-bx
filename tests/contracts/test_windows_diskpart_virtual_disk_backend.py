from __future__ import annotations

from pathlib import Path
from tests.fixtures.secure_script_io import benign_script_storage
import subprocess

import pytest


@pytest.fixture(autouse=True)
def secure_storage_model(monkeypatch, tmp_path):
    monkeypatch.setattr("rekordbox_usb.windows_diskpart_virtual_disk_backend.secure_diskpart_script",
                        benign_script_storage(tmp_path))

from rekordbox_usb.windows_diskpart_virtual_disk_backend import (
    DiskpartExecutionError,
    DiskpartVirtualDiskBackend,
    SubprocessDiskpartRunner,
    WindowsVirtualDiskBackendKind,
    select_windows_virtual_disk_backend,
)
from rekordbox_usb.windows_vhdx_nml_adapter import WindowsVhdxPathOutsideStorageError, WindowsVhdxVolumeInspection
from rekordbox_usb.windows_virtual_disk_backend import WindowsStorageElevationRequiredError
from rekordbox_usb.source_write_guard import (
    SourceWriteBoundary, SourceWriteBoundaryError, WriteDenial, WriteIntent, WritePlan,
)


class FakeDiskpartRunner:
    def __init__(self, available: bool = True, storage: "FakeStorageBackend | None" = None) -> None:
        self.available = available
        self.storage = storage
        self.scripts: list[str] = []

    def run(self, script: str, timeout_seconds: float) -> str:
        self.scripts.append(script)
        if self.storage is not None:
            if "attach vdisk" in script:
                self.storage.attached = True
            if "detach vdisk" in script:
                self.storage.attached = False
        return "ok"

    def is_available(self) -> bool:
        return self.available


class FakeStorageBackend:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[str] = []
        self.attached = False
        self.ownership = None

    def set_ownership_capability(self, capability) -> None:
        self.ownership = capability

    def inspect(self, path: Path) -> WindowsVhdxVolumeInspection:
        self.calls.append("inspect")
        return WindowsVhdxVolumeInspection(path, True, self.attached, True, "NTFS", "TRAKTORBOX_USB", Path("T:\\"))

    def initialize_if_needed(self, path: Path, timeout_seconds: float) -> None:
        self.calls.append("initialize")

    def format_if_needed(self, path: Path, filesystem: str, label: str, timeout_seconds: float) -> None:
        self.calls.append("format")

    def assign_mount(self, path: Path, preferred_drive_letter: str | None, timeout_seconds: float) -> Path:
        self.calls.append("mount")
        return Path("T:\\")


class FakePowerShellRunner:
    def __init__(self, output: str) -> None:
        self.output = output

    def run(self, script: str, timeout_seconds: float) -> str:
        return self.output


def backend(tmp_path: Path) -> tuple[DiskpartVirtualDiskBackend, FakeDiskpartRunner, FakeStorageBackend, Path]:
    root = tmp_path / "managed espaço ç"
    root.mkdir()
    path = root / "catálogo.vhdx"
    storage = FakeStorageBackend(path)
    diskpart = FakeDiskpartRunner(storage=storage)
    value = DiskpartVirtualDiskBackend(root, diskpart_runner=diskpart, storage_backend=storage)
    source = tmp_path / "synthetic-usb"
    source.mkdir(exist_ok=True)
    value.bind_write_boundary(SourceWriteBoundary(source))
    return value, diskpart, storage, path


class _Owned:
    consumed = True

    def __init__(self, action: str, path: Path) -> None:
        self.action = action
        self.path = path

    def assert_consumed(self, *, action: str, path: Path, current_identity) -> None:
        assert action == self.action and path == self.path


def allow(
    value: DiskpartVirtualDiskBackend,
    path: Path,
    action: str | None = None,
) -> None:
    value.set_write_permit(value._write_boundary.authorize(WritePlan(WriteIntent.VHDX, (path,))))
    if action is not None:
        value.set_ownership_capability(_Owned(action, path))  # type: ignore[arg-type]


def test_diskpart_create_attach_detach_are_path_scoped_and_safe(tmp_path: Path) -> None:
    value, runner, _, path = backend(tmp_path)
    allow(value, path)
    value.create_vhdx(path, 64, 10)
    allow(value, path, "attach")
    value.attach(path, 10)
    allow(value, path, "detach")
    value.detach(path, 10)
    assert runner.scripts[0] == f'create vdisk file="{path}" maximum=64 type=expandable\n'
    assert runner.scripts[1].startswith(f'select vdisk file="{path}"\nattach vdisk')
    assert runner.scripts[2].startswith(f'select vdisk file="{path}"\ndetach vdisk')
    assert len(runner.scripts) == 3
    forbidden = ("clean", "delete disk", "delete partition", "remove partition", "clear-disk", "d:\\", "collection.nml")
    assert not any(term in "\n".join(runner.scripts).casefold() for term in forbidden)


def test_diskpart_rejects_outside_path_and_delegates_storage_operations(tmp_path: Path) -> None:
    value, runner, storage, path = backend(tmp_path)
    with pytest.raises(WindowsVhdxPathOutsideStorageError):
        value.attach(tmp_path / "outside.vhdx", 10)
    assert not runner.scripts
    assert value.inspect(path).filesystem == "NTFS"
    allow(value, path, "initialize")
    value.initialize_if_needed(path, 10)
    allow(value, path, "format")
    value.format_if_needed(path, "NTFS", "TRAKTORBOX_USB", 10)
    allow(value, path, "assign_mount")
    assert value.assign_mount(path, "T", 10) == Path("T:\\")
    assert storage.calls == ["inspect", "initialize", "format", "mount"]


def test_diskpart_timeout_and_ambiguous_errors_are_not_silenced(tmp_path: Path) -> None:
    value, runner, _, path = backend(tmp_path)

    def timeout(script: str, timeout_seconds: float) -> str:
        raise DiskpartExecutionError("timed out")

    runner.run = timeout  # type: ignore[method-assign]
    allow(value, path, "attach")
    with pytest.raises(Exception, match="timed out"):
        value.attach(path, 1)

    allow(value, path, "detach")
    with pytest.raises(Exception, match="timed out"):
        value.detach(path, 1)


def test_diskpart_runner_removes_temporary_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = SubprocessDiskpartRunner()
    monkeypatch.setattr(SubprocessDiskpartRunner, "is_available", lambda self: True)

    def completed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        script = Path(args[0][2])  # type: ignore[index]
        assert script.exists()
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr("rekordbox_usb.windows_diskpart_virtual_disk_backend.subprocess.run", completed)
    source = tmp_path / "synthetic-usb"
    source.mkdir()
    boundary = SourceWriteBoundary(source)
    target = tmp_path / "safe.vhdx"
    for operation in ("attach", "detach"):
        permit = boundary.authorize(WritePlan(WriteIntent.VHDX, (target,)))
        runner.run(
            f'select vdisk file="{target}"\n{operation} vdisk\n', 1,
            boundary=boundary, vhdx_path=target, vhdx_permit=permit,
        )
        permit.close()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_diskpart_detach_uses_secure_storage_after_source_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = SubprocessDiskpartRunner()
    monkeypatch.setattr(SubprocessDiskpartRunner, "is_available", lambda self: True)

    def completed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        script = Path(args[0][2])  # type: ignore[index]
        assert script.parent != target.parent
        assert script.parent.name == "secure-runtime"
        assert script.name.startswith("diskpart-")
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(
        "rekordbox_usb.windows_diskpart_virtual_disk_backend.subprocess.run",
        completed,
    )
    source = tmp_path / "synthetic-usb"
    source.mkdir()
    target = tmp_path / "managed.vhdx"
    target.write_bytes(b"synthetic")
    initial = SourceWriteBoundary(source)
    binding = initial.capture_local_destination(target, intent=WriteIntent.VHDX)
    restarted = SourceWriteBoundary.from_local_binding(binding)
    permit = restarted.authorize(WritePlan(WriteIntent.VHDX, (target,)))

    runner.run(
        f'select vdisk file="{target}"\ndetach vdisk\n',
        1,
        boundary=restarted,
        vhdx_path=target,
        vhdx_permit=permit,
    )

    permit.close()
    assert target.read_bytes() == b"synthetic"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_diskpart_runner_maps_windows_elevation_error_and_removes_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = SubprocessDiskpartRunner()
    monkeypatch.setattr(SubprocessDiskpartRunner, "is_available", lambda self: True)

    def elevation_required(*_args: object, **_kwargs: object) -> None:
        error = OSError("operation requires elevation")
        error.winerror = 740  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(
        "rekordbox_usb.windows_diskpart_virtual_disk_backend.subprocess.run",
        elevation_required,
    )
    source = tmp_path / "synthetic-usb"
    source.mkdir()
    target = tmp_path / "managed.vhdx"
    boundary = SourceWriteBoundary(source)
    permit = boundary.authorize(WritePlan(WriteIntent.VHDX, (target,)))

    with pytest.raises(WindowsStorageElevationRequiredError):
        runner.run(
            f'select vdisk file="{target}"\nattach vdisk\n',
            1,
            boundary=boundary,
            vhdx_path=target,
            vhdx_permit=permit,
        )

    permit.close()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_diskpart_direct_backend_and_temp_inside_source_are_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    value, fake, _, path = backend(tmp_path)
    with pytest.raises(SourceWriteBoundaryError) as missing:
        value.attach(path, 5)
    assert missing.value.code is WriteDenial.PERMIT_MISMATCH
    assert not fake.scripts

    source = tmp_path / "synthetic-usb"
    runner = SubprocessDiskpartRunner()
    monkeypatch.setattr(SubprocessDiskpartRunner, "is_available", lambda self: True)
    with pytest.raises(SourceWriteBoundaryError) as inside_source:
        boundary = SourceWriteBoundary(source)
        target = source / "inside.vhdx"
        permit = boundary.authorize(WritePlan(WriteIntent.VHDX, (target,)))
        runner.run(
            f'select vdisk file="{target}"\nattach vdisk\n', 1, boundary=boundary,
            vhdx_path=target, vhdx_permit=permit,
        )
    assert inside_source.value.code is WriteDenial.DESTINATION_WITHIN_SOURCE
    assert list(source.iterdir()) == []


def test_diskpart_runner_rejects_unplanned_script_before_temp_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "synthetic-usb"
    source.mkdir()
    target = tmp_path / "managed.vhdx"
    boundary = SourceWriteBoundary(source)
    permit = boundary.authorize(WritePlan(WriteIntent.VHDX, (target,)))
    runner = SubprocessDiskpartRunner()
    monkeypatch.setattr(SubprocessDiskpartRunner, "is_available", lambda self: True)
    with pytest.raises(SourceWriteBoundaryError) as denied:
        runner.run(
            "clean\n", 1, boundary=boundary,
            vhdx_path=target, vhdx_permit=permit,
        )
    assert denied.value.code is WriteDenial.TARGET_NOT_IN_WRITE_PLAN
    assert list(tmp_path.glob(".*.tmp")) == []
    permit.close()


def test_capability_selection_preserves_hyper_v_and_chooses_diskpart_explicitly(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    hyper_v = select_windows_virtual_disk_backend(
        root, power_shell_runner=FakePowerShellRunner("[true,true,true]"), diskpart_runner=FakeDiskpartRunner()
    )
    assert hyper_v.kind is WindowsVirtualDiskBackendKind.HYPER_V
    fallback = select_windows_virtual_disk_backend(
        root,
        power_shell_runner=FakePowerShellRunner("[false,false,false]"),
        diskpart_runner=FakeDiskpartRunner(),
    )
    assert fallback.kind is WindowsVirtualDiskBackendKind.DISKPART
