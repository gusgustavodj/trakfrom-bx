"""Controlled, private-free tests for the explicit public command path."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from subprocess import run
import sys

import pytest

from rekordbox_usb.host_process import HostProcessState
from rekordbox_usb.public_cli import main
from rekordbox_usb.public_runtime import PublicRuntime, PublicRuntimeError
from rekordbox_usb.vhdx_ownership import VhdxFileIdentity, VhdxOwnershipLedger
from rekordbox_usb.windows_drives import DriveInfo
from rekordbox_usb.windows_vhdx_nml_adapter import WindowsVhdxVolumeInspection
from rekordbox_usb.windows_virtual_disk_backend import WindowsStorageElevationRequiredError
from rekordbox_usb.source_write_guard import SourceWriteBoundary, SourceWriteBoundaryError, WriteIntent, WritePlan
from tests.fixtures.synthetic_legacy_pdb import SyntheticLegacyPdbBuilder


class _Host:
    def state(self) -> HostProcessState:
        return HostProcessState.NOT_RUNNING


class _UncertainHost:
    def state(self) -> HostProcessState:
        return HostProcessState.UNKNOWN


class _Backend:
    def __init__(self, mount: Path) -> None:
        self.mount = mount
        self.state: WindowsVhdxVolumeInspection | None = None
        self.calls: list[str] = []

    def inspect(self, path: Path) -> WindowsVhdxVolumeInspection:
        state = self.state or WindowsVhdxVolumeInspection(
            path, False, False, False, None, None, None,
        )
        return replace(
            state, physical_identity=VhdxFileIdentity.observe(path) if state.exists else None,
        )

    def create_vhdx(self, path: Path, logical_size_mb: int, timeout_seconds: float) -> None:
        self.calls.append("create")
        path.touch(exist_ok=False)
        self.state = replace(self.inspect(path), exists=True)

    def attach(self, path: Path, timeout_seconds: float) -> None:
        self.calls.append("attach")
        self.state = replace(self.inspect(path), attached=True)

    def initialize_if_needed(self, path: Path, timeout_seconds: float) -> None:
        self.calls.append("initialize")
        self.state = replace(self.inspect(path), initialized=True, partition_present=True)

    def format_if_needed(self, path: Path, filesystem: str, label: str, timeout_seconds: float) -> None:
        self.calls.append("format")
        self.state = replace(self.inspect(path), filesystem=filesystem, volume_label=label)

    def assign_mount(self, path: Path, preferred_drive_letter: str | None, timeout_seconds: float) -> Path:
        self.calls.append("mount")
        self.mount.mkdir(exist_ok=True)
        self.state = replace(self.inspect(path), mount_point=self.mount)
        return self.mount

    def detach(self, path: Path, timeout_seconds: float) -> None:
        self.calls.append("detach")
        self.state = replace(self.inspect(path), attached=False)

    def remove_vhdx(self, path: Path) -> None:
        raise AssertionError("public unpublish must preserve the VHDX")


def _runtime(source: Path, backend: _Backend) -> PublicRuntime:
    return PublicRuntime(
        volumes=lambda: (DriveInfo(source, "Synthetic", "ABCD1234", "FAT32", 2),),
        backend_factory=lambda _: backend,
        host_probe=_Host(),
        require_windows=False,
    )


def _fixture(source: Path, bases: tuple[int, int] = (0, 1)) -> bytes:
    library = source / "PIONEER" / "rekordbox" / "export.pdb"
    library.parent.mkdir(parents=True)
    contents = source / "Contents"
    contents.mkdir()
    builder = SyntheticLegacyPdbBuilder()
    for track_id, title in enumerate(("One", "Two", "Three", "Four"), start=1):
        (contents / f"{title}.mp3").write_bytes(b"synthetic audio")
        builder.add_track(track_id, title, f"/Contents/{title}.mp3")
    builder.add_node(10, "First", sibling_position=0)
    builder.add_node(20, "Second", sibling_position=1)
    builder.add_playlist_entries(10, [1, 2], position_base=bases[0])
    builder.add_playlist_entries(20, [3, 4], position_base=bases[1])
    payload = builder.build()
    library.write_bytes(payload)
    return payload


@pytest.mark.parametrize("bases", ((0, 0), (1, 1), (0, 1)), ids=("zero", "one", "mixed"))
def test_public_legacy_publish_status_and_unpublish(tmp_path: Path, bases: tuple[int, int]) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    original = _fixture(source, bases)
    data_root = tmp_path / "data"
    data_root.mkdir()
    backend = _Backend(tmp_path / "mount")
    runtime = _runtime(source, backend)

    assert runtime.preflight(source, data_root).ready
    assert not (data_root / "managed").exists()
    assert runtime.status(source, data_root).state == "NO_PUBLICATION"
    published = runtime.publish(source, data_root)
    assert published.playlist_count == 2
    assert published.track_count == 4
    assert runtime.status(source, data_root).state == "ACTIVE"
    assert tuple((tmp_path / "mount").rglob("*.nml"))
    assert runtime.unpublish(source, data_root).state == "DETACHED"
    assert runtime.unpublish(source, data_root).state == "DETACHED"
    assert backend.calls.count("detach") == 1
    assert (source / "PIONEER" / "rekordbox" / "export.pdb").read_bytes() == original


@pytest.mark.parametrize("tamper", [None, "source_object", "parent_object"])
@pytest.mark.parametrize("operation", ["managed", "explicit"])
def test_detach_reproves_same_usb_after_native_path_change_and_refuses_replacement(tmp_path, tamper, operation):
    source = tmp_path / "usb"
    source.mkdir()
    original = _fixture(source)
    data = tmp_path / "data"
    backend = _Backend(tmp_path / "mount")
    runtime = _runtime(source, backend)
    publication = runtime.publish(source, data)
    identity = publication.source_id.split(":")[1]
    path = data / "managed" / (identity + ".vhdx")
    ledger = VhdxOwnershipLedger(path.parent)
    payload = json.loads(ledger.path.read_text())
    # Synthetic ledger fixture: stable USB object, stale Windows NT volume name.
    for binding in [payload["ledger_binding"], payload["lock_binding"],
                    next(iter(payload["records"].values()))["write_binding"]]:
        binding["source_identity"]["final_path"] = r"\Device\OldUsbVolume"
        binding["source_parts"] = ["device", "oldusbvolume"]
        if tamper == "source_object":
            binding["source_identity"]["file_id"] = "f0" * 16
        if tamper == "parent_object":
            binding["parent_identity"]["file_id"] = "f1" * 16
    ledger.path.write_text(json.dumps(payload), encoding="utf-8")
    stored = ledger.record(path).write_binding
    with pytest.raises(SourceWriteBoundaryError):
        SourceWriteBoundary.from_local_binding(stored).authorize(WritePlan(WriteIntent.VHDX, (path,)))
    before = ledger.path.read_bytes()
    def detach():
        return (runtime.detach_managed(publication.source_id, path, data) if operation == "managed"
                else runtime.unpublish(source, data))
    if tamper:
        with pytest.raises(PublicRuntimeError, match="SOURCE_SAFETY_BLOCK"):
            detach()
        assert "detach" not in backend.calls and ledger.path.read_bytes() == before
    else:
        result = detach()
        assert backend.calls.count("detach") == 1
        assert result.changed if operation == "managed" else result.state == "DETACHED"
        assert not backend.inspect(path).attached and path.is_file()
        assert ledger.record(path).state.value == "MANAGED_DETACHED"
    assert (source / "PIONEER/rekordbox/export.pdb").read_bytes() == original


def test_public_first_run_preflight_is_read_only_and_publish_bootstraps(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    original = _fixture(source)
    data_root = tmp_path / "fresh-data"
    backend = _Backend(tmp_path / "mount")
    factory_calls: list[Path] = []

    def backend_factory(storage_root: Path) -> _Backend:
        factory_calls.append(storage_root)
        assert storage_root.is_dir()
        return backend

    runtime = PublicRuntime(
        volumes=lambda: (DriveInfo(source, "Synthetic", "ABCD1234", "FAT32", 2),),
        backend_factory=backend_factory,
        host_probe=_Host(), require_windows=False,
    )
    assert runtime.preflight(source, data_root).ready
    assert not data_root.exists()
    assert not factory_calls
    published = runtime.publish(source, data_root)
    assert published.track_count == 4
    assert (data_root / "managed").is_dir()
    assert factory_calls
    assert (source / "PIONEER" / "rekordbox" / "export.pdb").read_bytes() == original


def test_public_restart_and_source_removal_keep_manual_lifecycle_available(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    _fixture(source)
    data_root = tmp_path / "data"
    backend = _Backend(tmp_path / "mount")
    first = _runtime(source, backend)
    first.publish(source, data_root)
    ledger_path = VhdxOwnershipLedger(data_root / "managed").path
    ledger_before_status = ledger_path.read_bytes()
    calls_before_status = tuple(backend.calls)
    restarted = _runtime(source, backend)
    assert restarted.status(source, data_root).state == "ACTIVE"
    assert ledger_path.read_bytes() == ledger_before_status
    assert tuple(backend.calls) == calls_before_status
    source.rename(tmp_path / "removed-usb")
    assert restarted.status(source, data_root).state == "ACTIVE"
    assert ledger_path.read_bytes() == ledger_before_status
    assert tuple(backend.calls) == calls_before_status
    assert restarted.unpublish(source, data_root).state == "DETACHED"
    assert restarted.status(source, data_root).state == "DETACHED"
    assert backend.calls.count("detach") == 1


def test_public_lifecycle_survives_new_process(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    _fixture(source)
    data_root = tmp_path / "data"
    mount = tmp_path / "mount"
    _runtime(source, _Backend(mount)).publish(source, data_root)
    public_src = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
from rekordbox_usb.public_runtime import PublicRuntime
from rekordbox_usb.windows_drives import DriveInfo
from rekordbox_usb.windows_vhdx_nml_adapter import WindowsVhdxVolumeInspection
from tests.contracts.test_public_executable import _Backend, _Host
source, data_root, mount = map(Path, sys.argv[3:6])
backend = _Backend(mount)
identity_id, _ = PublicRuntime._source_id('ABCD1234')
vhdx = data_root / 'managed' / (identity_id + '.vhdx')
backend.state = WindowsVhdxVolumeInspection(
    vhdx, True, True, True, 'NTFS', 'TRAKFROM_BX', mount,
)
runtime = PublicRuntime(
    volumes=lambda: (DriveInfo(source, 'Synthetic', 'ABCD1234', 'FAT32', 2),),
    backend_factory=lambda _: backend, host_probe=_Host(), require_windows=False,
)
assert runtime.status(source, data_root).state == 'ACTIVE'
assert runtime.unpublish(source, data_root).state == 'DETACHED'
assert backend.calls.count('detach') == 1
print('new_process_lifecycle_ok')
"""
    result = run(
        [sys.executable, "-I", "-B", "-c", script, str(public_src),
         str(public_src.parent), str(source), str(data_root), str(mount)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "new_process_lifecycle_ok" in result.stdout
    identity_id, _ = PublicRuntime._source_id("ABCD1234")
    ledger = VhdxOwnershipLedger(data_root / "managed")
    record = ledger.record(data_root / "managed" / f"{identity_id}.vhdx")
    assert record is not None and record.state == "MANAGED_DETACHED"


def test_public_help_is_side_effect_free(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_root = tmp_path / "data"
    with pytest.raises(SystemExit) as result:
        main(("--help",))
    captured = capsys.readouterr()
    assert result.value.code == 0
    assert "preflight" in captured.out and "unpublish" in captured.out
    assert captured.err == ""
    assert not data_root.exists()


def test_public_failed_bootstrap_can_retry_without_active_claim(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    original = _fixture(source)
    data_root = tmp_path / "data"
    backend = _Backend(tmp_path / "mount")
    attempts = 0

    def factory(storage_root: Path) -> _Backend:
        nonlocal attempts
        assert storage_root.is_dir()
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic backend construction failure")
        return backend

    runtime = PublicRuntime(
        volumes=lambda: (DriveInfo(source, "Synthetic", "ABCD1234", "FAT32", 2),),
        backend_factory=factory, host_probe=_Host(), require_windows=False,
    )
    with pytest.raises(PublicRuntimeError, match="VHDX_PRECONDITION_FAILURE"):
        runtime.publish(source, data_root)
    assert not tuple((data_root / "managed").glob("*.vhdx"))
    assert not VhdxOwnershipLedger(data_root / "managed").path.exists()
    assert runtime.preflight(source, data_root).ready
    assert runtime.publish(source, data_root).track_count == 4
    assert (source / "PIONEER" / "rekordbox" / "export.pdb").read_bytes() == original


@pytest.mark.parametrize("corruption", ("truncated", "partial", "identity", "path"))
def test_public_invalid_ownership_fails_closed(tmp_path: Path, corruption: str) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    _fixture(source)
    data_root = tmp_path / "data"
    backend = _Backend(tmp_path / "mount")
    runtime = _runtime(source, backend)
    runtime.publish(source, data_root)
    identity_id, _ = runtime._source_id("ABCD1234")
    storage_root, vhdx_path = runtime._paths(data_root, identity_id)
    ledger = VhdxOwnershipLedger(storage_root)
    if corruption == "truncated":
        ledger.path.write_bytes(b"{")
    elif corruption == "partial":
        payload = json.loads(ledger.path.read_text(encoding="utf-8"))
        only_record = next(iter(payload["records"].values()))
        only_record["state"] = "PARTIAL"
        ledger.path.write_text(json.dumps(payload), encoding="utf-8")
    elif corruption == "path":
        payload = json.loads(ledger.path.read_text(encoding="utf-8"))
        only_record = next(iter(payload["records"].values()))
        only_record["path"] = str(storage_root / "other.vhdx")
        ledger.path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        vhdx_path.rename(storage_root / "old.vhdx")
        vhdx_path.write_bytes(b"foreign replacement")
    with pytest.raises(PublicRuntimeError):
        runtime.status(source, data_root)
    with pytest.raises(PublicRuntimeError):
        runtime.unpublish(source, data_root)
    assert "detach" not in backend.calls


def test_public_cli_and_fail_closed_inputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    backend = _Backend(tmp_path / "mount")
    runtime = _runtime(source, backend)
    assert main(("preflight", "--volume", str(source), "--data-root", str(data_root), "--json"), runtime=runtime) == 3
    assert "UNSUPPORTED_LIBRARY" in capsys.readouterr().out
    with pytest.raises(SystemExit) as invalid:
        main(("invalid", "--volume", str(source), "--data-root", str(data_root)), runtime=runtime)
    assert invalid.value.code == 2
    _fixture(source)
    assert main(("preflight", "--volume", str(source), "--data-root", str(data_root)), runtime=runtime) == 0
    assert main(("status", "--volume", str(source), "--data-root", str(data_root)), runtime=runtime) == 0
    assert "NO_PUBLICATION" in capsys.readouterr().out
    assert main(("publish", "--volume", str(source), "--data-root", str(data_root)), runtime=runtime) == 0
    assert main(("unpublish", "--volume", str(source), "--data-root", str(data_root)), runtime=runtime) == 0


def test_public_rejects_foreign_vhdx(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    _fixture(source)
    data_root = tmp_path / "data"
    data_root.mkdir()
    backend = _Backend(tmp_path / "mount")
    runtime = _runtime(source, backend)
    identity_id, _ = runtime._source_id("ABCD1234")
    managed, vhdx = runtime._paths(data_root, identity_id)
    managed.mkdir()
    vhdx.touch()
    backend.state = WindowsVhdxVolumeInspection(vhdx, True, True, True, "NTFS", "TRAKFROM_BX", tmp_path / "mount")
    assert runtime.preflight(source, data_root).code == "OWNERSHIP_FAILURE"
    with pytest.raises(PublicRuntimeError, match="OWNERSHIP_FAILURE"):
        runtime.unpublish(source, data_root)
    assert "detach" not in backend.calls


def test_public_preflight_and_publish_failures(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    backend = _Backend(tmp_path / "mount")
    runtime = _runtime(source, backend)
    assert runtime.preflight(source, data_root).code == "UNSUPPORTED_LIBRARY"
    assert runtime.preflight(tmp_path / "absent", data_root).code == "SOURCE_UNAVAILABLE"
    _fixture(source)
    nested_data_root = source / "data"
    nested_data_root.mkdir()
    assert runtime.preflight(source, nested_data_root).code == "SOURCE_SAFETY_BLOCK"
    unavailable = PublicRuntime(
        volumes=lambda: (DriveInfo(source, "Synthetic", "ABCD1234", "FAT32", 2),),
        backend_factory=lambda _: (_ for _ in ()).throw(RuntimeError("missing tool")),
        capability_probe=lambda: False,
        host_probe=_Host(), require_windows=False,
    )
    assert unavailable.preflight(source, data_root).code == "VHDX_PRECONDITION_FAILURE"
    elevation = PublicRuntime(
        volumes=lambda: (DriveInfo(source, "Synthetic", "ABCD1234", "FAT32", 2),),
        backend_factory=lambda _: (_ for _ in ()).throw(WindowsStorageElevationRequiredError("elevation")),
        capability_probe=lambda: (_ for _ in ()).throw(WindowsStorageElevationRequiredError("elevation")),
        host_probe=_Host(), require_windows=False,
    )
    assert main(("preflight", "--volume", str(source), "--data-root", str(data_root)), runtime=elevation) == 4
    invalid = SyntheticLegacyPdbBuilder().add_node(1, "Set").add_entry(1, 99, 0).build()
    (source / "PIONEER" / "rekordbox" / "export.pdb").write_bytes(invalid)
    with pytest.raises(PublicRuntimeError, match="INVALID_LIBRARY"):
        runtime.publish(source, data_root)
    assert not backend.calls


def test_public_publish_blocks_unknown_host_before_output(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    _fixture(source)
    data_root = tmp_path / "data"
    data_root.mkdir()
    backend = _Backend(tmp_path / "mount")
    runtime = PublicRuntime(
        volumes=lambda: (DriveInfo(source, "Synthetic", "ABCD1234", "FAT32", 2),),
        backend_factory=lambda _: backend,
        host_probe=_UncertainHost(), require_windows=False,
    )
    with pytest.raises(PublicRuntimeError, match="HOST_NOT_CLEAR"):
        runtime.publish(source, data_root)
    assert not (data_root / "managed").exists()
    assert not backend.calls


def test_public_unpublish_remains_available_after_library_becomes_invalid(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    _fixture(source)
    data_root = tmp_path / "data"
    data_root.mkdir()
    backend = _Backend(tmp_path / "mount")
    runtime = _runtime(source, backend)
    runtime.publish(source, data_root)
    (source / "PIONEER" / "rekordbox" / "export.pdb").write_bytes(b"changed externally")
    assert runtime.preflight(source, data_root).code == "UNSUPPORTED_LIBRARY"
    assert runtime.status(source, data_root).state == "ACTIVE"
    assert runtime.unpublish(source, data_root).state == "DETACHED"


def test_public_core_imports_with_private_package_blocked() -> None:
    public_src = Path(__file__).resolve().parents[2] / "src"
    script = """
import importlib.abc, importlib.util, sys, tempfile
from pathlib import Path
class BlockPrivate(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'rekordbox_usb_internal' or fullname.startswith('rekordbox_usb_internal.'):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, BlockPrivate())
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
from rekordbox_usb.public_runtime import PublicRuntime
from rekordbox_usb.public_cli import main
from rekordbox_usb.windows_drives import DriveInfo
from tests.contracts.test_public_executable import _Backend, _Host, _fixture
assert 'rekordbox_usb_internal' not in sys.modules
try:
    main(['--help'])
except SystemExit as result:
    assert result.code == 0
else:
    raise AssertionError('help did not exit')
assert main(['preflight', '--volume', 'Z:/absent', '--data-root', 'Z:/absent'], runtime=PublicRuntime(require_windows=False)) == 3
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    source = root / 'usb'
    source.mkdir()
    _fixture(source)
    data_root = root / 'fresh-data'
    backend = _Backend(root / 'mount')
    runtime = PublicRuntime(
        volumes=lambda: (DriveInfo(source, 'Synthetic', 'ABCD1234', 'FAT32', 2),),
        backend_factory=lambda _: backend,
        host_probe=_Host(), require_windows=False,
    )
    options = ['--volume', str(source), '--data-root', str(data_root)]
    assert main(['preflight', *options], runtime=runtime) == 0
    assert not data_root.exists()
    assert main(['publish', *options], runtime=runtime) == 0
    assert main(['status', *options], runtime=runtime) == 0
    assert main(['unpublish', *options], runtime=runtime) == 0
assert 'rekordbox_usb_internal' not in sys.modules
print('private_absent_public_core_ok')
"""
    result = run(
        [sys.executable, "-I", "-B", "-c", script, str(public_src), str(public_src.parent)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "private_absent_public_core_ok" in result.stdout
