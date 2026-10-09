"""Synthetic public USB selection and read-only preflight contracts."""

import builtins
import io
import json
from pathlib import Path

import pytest

from rekordbox_usb.public_cli import main
from rekordbox_usb.public_runtime import PublicRuntime, PublicRuntimeError
from rekordbox_usb.windows_drives import DriveInfo, list_windows_volumes
from tests.contracts.test_public_executable import _Backend, _Host, _fixture
from tests.contracts.test_windows_drives import FakeWindowsVolumeApi


def runtime_for(volumes):
    return PublicRuntime(
        volumes=lambda: tuple(volumes), require_windows=False,
        capability_probe=lambda: True, host_probe=_Host(),
        backend_factory=lambda _: (_ for _ in ()).throw(AssertionError("backend mutation")),
    )


def source(tmp_path, name="usb", **metadata):
    root = tmp_path / name
    root.mkdir()
    _fixture(root)
    return DriveInfo(root, metadata.get("label", "Synthetic"), name.upper(),
                     "FAT32", metadata.get("drive_type", 2), metadata.get("bus_type"))


@pytest.mark.parametrize("count,code", [(0, "NO_REKORDBOX_USB"), (2, "MULTIPLE_REKORDBOX_USB")])
def test_zero_and_multiple_block_publication(tmp_path, capsys, count, code):
    volumes = [source(tmp_path, f"usb{i}") for i in range(count)]
    runtime = runtime_for(volumes)
    for command in ("preflight", "publish", "status", "unpublish"):
        assert main([command, "--data-root", str(tmp_path / "data"), "--json"], runtime=runtime) == 3
        payload = json.loads(capsys.readouterr().out)
        assert payload["code"] == code
        if count:
            assert "--volume <volume>" in payload["message"]
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("drive_type,bus_type", [(2, None), (2, 7), (3, 7)])
def test_single_source_json_identity_and_no_writes(tmp_path, capsys, monkeypatch, drive_type, bus_type):
    volume = source(tmp_path, drive_type=drive_type, bus_type=bus_type)
    runtime = runtime_for([volume])
    before = {path: path.read_bytes() for path in volume.root.rglob("*") if path.is_file()}
    original_open, original_io_open = builtins.open, io.open

    def read_only_opener(opener):
        def guarded(file, mode="r", *args, **kwargs):
            assert not any(flag in mode for flag in "wax+"), "write attempted during preflight"
            return opener(file, mode, *args, **kwargs)
        return guarded

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", read_only_opener(original_open))
        guard.setattr(io, "open", read_only_opener(original_io_open))
        for method in ("mkdir", "touch", "write_bytes", "write_text", "unlink", "rename", "replace", "rmdir"):
            guard.setattr(Path, method, lambda *a, **k: pytest.fail("filesystem mutation"))
        assert main(["preflight", "--data-root", str(tmp_path / "data"), "--json"], runtime=runtime) == 0
        automatic = json.loads(capsys.readouterr().out)
        assert main(["preflight", "--volume", str(volume.root), "--data-root",
                     str(tmp_path / "data"), "--json"], runtime=runtime) == 0
        explicit = json.loads(capsys.readouterr().out)
    assert automatic == explicit == {"command": "preflight", "code": "READY", "ready": True,
                                    "source_id": runtime._source_id(volume.serial)[1]}
    assert before == {path: path.read_bytes() for path in volume.root.rglob("*") if path.is_file()}
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("metadata", [
    {"drive_type": 3}, {"drive_type": 3, "bus_type": 11},
    {"drive_type": 2, "bus_type": 14}, {"drive_type": 2, "bus_type": 15},
    {"drive_type": 3, "bus_type": 15}, {"label": "TRAKFROM_BX", "bus_type": 7},
    {"label": "TRAKTORBOX"}, {"drive_type": 4}, {"drive_type": 5},
])
def test_internal_virtual_managed_and_nonlocal_excluded(tmp_path, metadata):
    excluded = source(tmp_path, "excluded", **metadata)
    valid = source(tmp_path)
    runtime = runtime_for([excluded, valid])
    assert runtime.resolve_source(None, tmp_path / "data") == valid.root
    with pytest.raises(PublicRuntimeError, match="NO_REKORDBOX_USB"):
        runtime_for([excluded]).resolve_source(None, tmp_path / "data")


def test_destination_and_missing_identity_excluded(tmp_path):
    volume = source(tmp_path)
    with pytest.raises(PublicRuntimeError, match="NO_REKORDBOX_USB"):
        runtime_for([volume]).resolve_source(None, volume.root)
    no_identity = DriveInfo(volume.root, "USB", None, "FAT32", 2)
    with pytest.raises(PublicRuntimeError, match="NO_REKORDBOX_USB"):
        runtime_for([no_identity]).resolve_source(None, tmp_path / "data")


def test_override_bypasses_ambiguity_and_invalid_volume_still_fails(tmp_path, capsys):
    first, second = source(tmp_path), source(tmp_path, "other")
    runtime = runtime_for([first, second])
    for path, code, exit_code in [(first.root, "READY", 0), (tmp_path / "absent", "SOURCE_UNAVAILABLE", 3)]:
        assert main(["preflight", "--volume", str(path), "--data-root", str(tmp_path / "data"),
                     "--json"], runtime=runtime) == exit_code
        assert json.loads(capsys.readouterr().out)["code"] == code


def test_discovery_failure_is_sanitized(tmp_path, capsys):
    runtime = PublicRuntime(volumes=lambda: (_ for _ in ()).throw(OSError("private details")),
                            require_windows=False)
    assert main(["preflight", "--data-root", str(tmp_path / "data"), "--json"], runtime=runtime) == 3
    assert json.loads(capsys.readouterr().out) == {"command": "preflight", "code": "SOURCE_DISCOVERY_FAILURE"}


def test_selected_corrupt_library_uses_normal_preflight(tmp_path, capsys):
    volume = source(tmp_path)
    (volume.root / "PIONEER/rekordbox/export.pdb").write_bytes(b"invalid synthetic library")
    assert main(["preflight", "--data-root", str(tmp_path / "data"), "--json"],
                runtime=runtime_for([volume])) == 3
    assert json.loads(capsys.readouterr().out)["code"] == "UNSUPPORTED_LIBRARY"


def test_help_volume_optional(capsys):
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    assert "[--volume VOLUME]" in capsys.readouterr().out


def test_existing_enumerator_propagates_bus_metadata():
    class Api(FakeWindowsVolumeApi):
        def bus_type(self, root):
            return {"C:\\": 11, "D:\\": 7, "N:\\": None}[root]
    assert [item.bus_type for item in list_windows_volumes(Api())] == [11, 7, None]
