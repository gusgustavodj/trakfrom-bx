"""Synthetic safety contracts for the authorial P0-04 guard."""

from __future__ import annotations

import ctypes
import ntpath
from pathlib import Path

import pytest

import rekordbox_usb.source_write_guard as source_write_guard
from rekordbox_usb.source_write_guard import (
    CanonicalPathIdentity,
    SourceWriteBoundary,
    SourceWriteBoundaryError,
    WriteDenial,
    WriteIntent,
    WritePlan,
    atomic_write_bytes,
)


class _LegacyIdentityKernel:
    def GetFileInformationByHandleEx(self, *_args: object) -> bool:
        return False

    def GetFileInformationByHandle(self, _handle: int, pointer: object) -> bool:
        value = ctypes.cast(
            pointer,
            ctypes.POINTER(source_write_guard._ByHandleFileInformation),
        ).contents
        value.volume_serial = 0x1234ABCD
        value.file_index_high = 0x01020304
        value.file_index_low = 0xA0B0C0D0
        return True


def _key(path: Path | str) -> str:
    return ntpath.normcase(ntpath.normpath(str(path)))


class FakeAdapter:
    def __init__(self) -> None:
        self.entries: dict[str, CanonicalPathIdentity] = {}
        self.unsafe: set[str] = set()

    def add(self, path: str, final: str, file_id: int, volume: int = 1) -> None:
        self.entries[_key(path)] = CanonicalPathIdentity(final, volume, file_id.to_bytes(16, "little"))

    def observe(self, path: Path) -> CanonicalPathIdentity | None:
        key = _key(path)
        if key in self.unsafe:
            raise SourceWriteBoundaryError(WriteDenial.REPARSE_IDENTITY_UNSAFE)
        return self.entries.get(key)


def _adapter() -> FakeAdapter:
    adapter = FakeAdapter()
    adapter.add("E:\\", "\\Device\\VolumeE\\", 1)
    adapter.add("Z:\\", "\\Device\\VolumeE\\", 1)
    adapter.add("C:\\", "\\Device\\VolumeC\\", 2, 2)
    adapter.add("C:\\work", "\\Device\\VolumeC\\work", 3, 2)
    adapter.add("C:\\MUSIC", "\\Device\\VolumeC\\MUSIC", 4, 2)
    return adapter


def test_windows_identity_falls_back_when_file_id_info_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = object.__new__(source_write_guard.WindowsPathIdentityAdapter)
    adapter._kernel = _LegacyIdentityKernel()
    monkeypatch.setattr(source_write_guard.ctypes, "get_last_error", lambda: 87)

    volume_serial, file_id = adapter._identity_for_handle(123)

    assert volume_serial == 0x1234ABCD
    assert file_id.startswith(b"BY_HANDLE_FILE_INFORMATION\x00")
    assert file_id.endswith(bytes.fromhex("04030201d0c0b0a0"))


def _authorize(
    source: str, target: str, *, adapter: FakeAdapter | None = None,
    base: str | None = None, other: tuple[str, ...] = (),
):
    adapter = adapter or _adapter()
    boundary = SourceWriteBoundary(
        Path(source), adapter=adapter,
        approved_destination_base=Path(base) if base else None,
    )
    plan = WritePlan(WriteIntent.STATE, tuple(Path(p) for p in (target, *other)))
    return boundary, boundary.authorize(plan), adapter


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("E:\\", WriteDenial.DESTINATION_EQUALS_SOURCE),
        ("E:\\child.json", WriteDenial.DESTINATION_WITHIN_SOURCE),
        ("E:\\deep\\child.json", WriteDenial.DESTINATION_WITHIN_SOURCE),
        ("e:\\CHILD.json", WriteDenial.DESTINATION_WITHIN_SOURCE),
        ("Z:\\child.json", WriteDenial.DESTINATION_WITHIN_SOURCE),
        ("C:\\work\\collection.nml", WriteDenial.PROTECTED_COLLECTION_NML),
        ("C:\\work\\COLLECTION.NML", WriteDenial.PROTECTED_COLLECTION_NML),
        ("relative.json", WriteDenial.RELATIVE_DESTINATION),
        ("C:relative.json", WriteDenial.RELATIVE_DESTINATION),
        ("C:\\work\\..\\other.json", WriteDenial.INVALID_DESTINATION),
        ("C:\\work\\out.json:stream", WriteDenial.INVALID_DESTINATION),
    ],
)
def test_denies_unsafe_destination(target: str, code: WriteDenial) -> None:
    with pytest.raises(SourceWriteBoundaryError) as error:
        _authorize("E:\\", target)
    assert error.value.code is code


@pytest.mark.parametrize("target", ["C:\\work\\out.json", "C:\\MUSIC_BACKUP\\out.json"])
def test_allows_external_and_prefix_collision_sibling(target: str) -> None:
    boundary, permit, _ = _authorize("C:\\MUSIC", target)
    assert boundary.source_root == Path("C:\\MUSIC")
    permit.revalidate_before_side_effect(Path(target), intent=WriteIntent.STATE)


def test_relative_requires_explicit_safe_base() -> None:
    _, permit, _ = _authorize("E:\\", "out.json", base="C:\\work")
    permit.revalidate_before_side_effect(Path("out.json"), intent=WriteIntent.STATE)


@pytest.mark.parametrize("role", ["temp.tmp", "backup.bak", "parents\\new"])
def test_one_unsafe_auxiliary_denies_whole_plan(role: str) -> None:
    with pytest.raises(SourceWriteBoundaryError) as error:
        _authorize("E:\\", "C:\\work\\safe.json", other=(f"E:\\{role}",))
    assert error.value.code is WriteDenial.DESTINATION_WITHIN_SOURCE


@pytest.mark.parametrize("alias", ["C:\\work\\link", "C:\\work\\junction"])
def test_ambiguous_reparse_denied(alias: str) -> None:
    adapter = _adapter()
    adapter.unsafe.add(_key(alias))
    with pytest.raises(SourceWriteBoundaryError) as error:
        _authorize("E:\\", alias, adapter=adapter)
    assert error.value.code is WriteDenial.REPARSE_IDENTITY_UNSAFE


def test_protected_collection_hardlink_identity_denied() -> None:
    adapter = _adapter()
    adapter.add("C:\\work\\collection.nml", "\\Device\\VolumeC\\work\\collection.nml", 10, 2)
    adapter.add("C:\\work\\alias.dat", "\\Device\\VolumeC\\work\\alias.dat", 10, 2)
    boundary = SourceWriteBoundary(
        Path("E:\\"), adapter=adapter,
        protected_collection_paths=(Path("C:\\work\\collection.nml"),),
    )
    with pytest.raises(SourceWriteBoundaryError) as error:
        boundary.authorize(WritePlan(WriteIntent.STATE, (Path("C:\\work\\alias.dat"),)))
    assert error.value.code is WriteDenial.PROTECTED_COLLECTION_NML


def test_source_identity_change_after_authorization_denied() -> None:
    _, permit, adapter = _authorize("E:\\", "C:\\work\\out.json")
    adapter.add("E:\\", "\\Device\\VolumeOther\\", 20, 20)
    with pytest.raises(SourceWriteBoundaryError) as error:
        permit.revalidate_before_side_effect(Path("C:\\work\\out.json"))
    assert error.value.code is WriteDenial.PATH_IDENTITY_CHANGED


def test_parent_identity_change_after_authorization_denied() -> None:
    _, permit, adapter = _authorize("E:\\", "C:\\work\\out.json")
    adapter.add("C:\\work", "\\Device\\VolumeC\\work", 30, 2)
    with pytest.raises(SourceWriteBoundaryError) as error:
        permit.revalidate_before_side_effect(Path("C:\\work\\out.json"))
    assert error.value.code is WriteDenial.PATH_IDENTITY_CHANGED


def test_permit_rejects_wrong_target_intent_and_reuse() -> None:
    _, permit, _ = _authorize("E:\\", "C:\\work\\out.json")
    with pytest.raises(SourceWriteBoundaryError) as target_error:
        permit.revalidate_before_side_effect(Path("C:\\work\\other.json"))
    assert target_error.value.code is WriteDenial.TARGET_NOT_IN_WRITE_PLAN
    with pytest.raises(SourceWriteBoundaryError) as intent_error:
        permit.revalidate_before_side_effect(Path("C:\\work\\out.json"), intent=WriteIntent.CACHE)
    assert intent_error.value.code is WriteDenial.PERMIT_MISMATCH
    permit.close()
    with pytest.raises(SourceWriteBoundaryError) as stale_error:
        permit.revalidate_before_side_effect(Path("C:\\work\\out.json"))
    assert stale_error.value.code is WriteDenial.PERMIT_MISMATCH


def test_path_appearing_after_authorize_is_denied_before_side_effect() -> None:
    _, permit, adapter = _authorize("E:\\", "C:\\work\\out.json")
    adapter.add("C:\\work\\out.json", "\\Device\\VolumeC\\work\\out.json", 31, 2)
    with pytest.raises(SourceWriteBoundaryError) as error:
        permit.revalidate_before_side_effect(Path("C:\\work\\out.json"))
    assert error.value.code is WriteDenial.PATH_IDENTITY_CHANGED


def test_atomic_writer_denies_source_child_before_parent_or_temp(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    target = source / "new" / "catalog.nml"
    with pytest.raises(SourceWriteBoundaryError) as error:
        atomic_write_bytes(
            SourceWriteBoundary(source), target, b"synthetic",
            intent=WriteIntent.NML_EXPORT,
        )
    assert error.value.code is WriteDenial.DESTINATION_WITHIN_SOURCE
    assert not target.parent.exists()
    assert not list(source.glob("*.tmp"))


def test_atomic_writer_writes_only_external_target(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    target = tmp_path / "local" / "catalog.nml"
    atomic_write_bytes(
        SourceWriteBoundary(source), target, b"synthetic",
        intent=WriteIntent.NML_EXPORT,
    )
    assert target.read_bytes() == b"synthetic"
    assert list(source.iterdir()) == []


def test_captured_local_file_allows_only_that_file_after_source_disappears(
    tmp_path: Path,
) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    target = tmp_path / "local" / "status.json"
    target.parent.mkdir()
    boundary = SourceWriteBoundary(source)
    binding = boundary.capture_local_destination(target)
    source.rmdir()

    detached = SourceWriteBoundary.from_local_binding(binding)
    atomic_write_bytes(detached, target, b"local", intent=WriteIntent.STATE)
    assert target.read_bytes() == b"local"
    with pytest.raises(SourceWriteBoundaryError) as error:
        detached.authorize(WritePlan(WriteIntent.STATE, (target.parent / "other.json",)))
    assert error.value.code is WriteDenial.TARGET_NOT_IN_WRITE_PLAN


def test_captured_local_file_denies_replaced_source(tmp_path: Path) -> None:
    source = tmp_path / "usb"
    source.mkdir()
    target = tmp_path / "status.json"
    binding = SourceWriteBoundary(source).capture_local_destination(target)
    source.rmdir()
    source.mkdir()
    with pytest.raises(SourceWriteBoundaryError) as error:
        atomic_write_bytes(
            SourceWriteBoundary.from_local_binding(binding), target,
            b"unsafe", intent=WriteIntent.STATE,
        )
    assert error.value.code is WriteDenial.PATH_IDENTITY_CHANGED
    assert not target.exists()
