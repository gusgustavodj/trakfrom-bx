from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import inspect
import json
from pathlib import Path
from pathlib import PureWindowsPath

import pytest

from rekordbox_usb.nml_catalog_materializer import (
    EscapingNmlCatalogPathError,
    InvalidNmlCatalogTargetError,
    NmlCatalogManifestError,
    NmlCatalogSourceMismatchError,
    NmlCatalogStagingError,
    UnmanagedNmlCatalogContentError,
    materialize_nml_catalog,
)
from rekordbox_usb.virtual_nml_catalog import NmlCatalog, NmlCatalogEntry
from rekordbox_usb.virtual_projection import VirtualPath


def _entry(path: VirtualPath, content: str = "<NML />\n", *, playlist_id: str = "p1") -> NmlCatalogEntry:
    return NmlCatalogEntry("source", playlist_id, path.segments[-1], "Nome", path, content, sha256(content.encode()).hexdigest(), 0, "revision")


def _catalog(*entries: NmlCatalogEntry, revision: str = "revision") -> NmlCatalog:
    return NmlCatalog("source", revision, tuple(replace(entry, snapshot_id=revision) for entry in entries))


def _materialize(catalog, target_root: Path, **kwargs):
    source_root = target_root.parent.resolve() / "synthetic-usb"
    source_root.mkdir(exist_ok=True)
    return materialize_nml_catalog(catalog, target_root, source_root=source_root, **kwargs)


def test_materializes_empty_and_hierarchical_catalogs_with_manifest(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    empty = _materialize(_catalog(), root, expected_source_id="source")
    assert empty.changed is True
    manifest = json.loads((root / "nml-catalog-manifest.json").read_text())
    assert manifest["entries"] == []

    path = VirtualPath(("playlists", "Pasta", "Café.nml"))
    second_path = VirtualPath(("playlists", "Pasta", "Ångström.nml"))
    result = _materialize(_catalog(_entry(path), _entry(second_path, playlist_id="p2")), root)
    assert result.changed is True
    assert (root / "playlists" / "Pasta" / "Café.nml").read_text() == "<NML />\n"
    assert (root / "playlists" / "Pasta" / "Ångström.nml").exists()
    assert len(result.content_hashes) == 2


def test_same_catalog_is_noop_and_changed_revision_replaces_complete_catalog(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    old_path = VirtualPath(("playlists", "old.nml"))
    catalog = _catalog(_entry(old_path))
    _materialize(catalog, root)
    unchanged = _materialize(catalog, root)
    assert unchanged.changed is False
    assert unchanged.preserved_paths == (old_path,)

    new_path = VirtualPath(("playlists", "new.nml"))
    changed = _materialize(_catalog(_entry(new_path, "<NML>new</NML>\n"), revision="next"), root)
    assert changed.changed is True
    assert changed.removed_paths == (old_path,)
    assert not (root / "playlists" / "old.nml").exists()
    assert (root / "playlists" / "new.nml").exists()


def test_managed_legacy_playlists_root_migrates_to_the_new_visual_root(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    legacy = _catalog(_entry(VirtualPath(("playlists", "set.nml"))))
    _materialize(legacy, root)
    renamed = _catalog(_entry(VirtualPath(("GUS CULT 25", "set.nml"))))

    migrated = _materialize(renamed, root)
    unchanged = _materialize(renamed, root)

    assert migrated.changed
    assert not (root / "playlists").exists()
    assert (root / "GUS CULT 25" / "set.nml").is_file()
    assert unchanged.changed is False


def test_unknown_legacy_playlists_root_is_never_removed(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    legacy = root / "playlists"
    legacy.mkdir(parents=True)
    (legacy / "foreign.nml").write_text("foreign")

    with pytest.raises(UnmanagedNmlCatalogContentError):
        _materialize(_catalog(_entry(VirtualPath(("GUS CULT 25", "set.nml")))), root)

    assert (legacy / "foreign.nml").read_text() == "foreign"


def test_unmanaged_content_blocks_replacement_and_staging_failure_preserves_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    (root / "foreign.txt").write_text("keep")
    with pytest.raises(UnmanagedNmlCatalogContentError):
        _materialize(_catalog(_entry(VirtualPath(("playlists", "one.nml")))), root)

    root = tmp_path / "managed"
    original = _catalog(_entry(VirtualPath(("playlists", "old.nml"))))
    _materialize(original, root)
    monkeypatch.setattr("rekordbox_usb.nml_catalog_materializer._write_file", lambda *_: (_ for _ in ()).throw(OSError("fail")))
    with pytest.raises(NmlCatalogStagingError):
        _materialize(
            _catalog(_entry(VirtualPath(("playlists", "new.nml"))), revision="next"), root
        )
    assert (root / "playlists" / "old.nml").exists()
    assert not list(root.parent.glob(".managed.staging-*"))


def test_empty_existing_root_allows_initial_materialization(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    result = _materialize(_catalog(_entry(VirtualPath(("playlists", "one.nml")))), root)
    assert result.changed
    assert (root / "nml-catalog-manifest.json").is_file()


def test_windows_volume_root_uses_reserved_direct_children() -> None:
    import rekordbox_usb.nml_catalog_materializer as materializer

    root = PureWindowsPath("E:/")
    assert materializer._is_volume_root(root)
    staging, backup = materializer._temporary_roots(root)
    assert staging.parent == root and backup.parent == root
    assert staging.name.startswith(".traktorbox-staging-")
    assert backup.name.startswith(".traktorbox-backup-")


@pytest.mark.parametrize(
    "administrative_names",
    [
        ("System Volume Information",),
        ("$RECYCLE.BIN",),
        ("system volume information", "$recycle.bin"),
    ],
)
def test_administrative_root_entries_allow_initial_materialization(
    tmp_path: Path, administrative_names: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    from rekordbox_usb.source_write_guard import WindowsPathIdentityAdapter

    root = tmp_path / "catalog"
    root.mkdir()
    administrative_entries = []
    for name in administrative_names:
        entry = root / name
        entry.mkdir()
        (entry / "untouched.txt").write_text("administrative")
        administrative_entries.append(entry)

    original_rglob = Path.rglob
    original_observe = WindowsPathIdentityAdapter.observe

    def reject_administrative_recursion(path: Path, pattern: str):
        if path.name.casefold() in {"system volume information", "$recycle.bin"}:
            raise AssertionError("administrative entry must not be traversed")
        return original_rglob(path, pattern)

    def reject_administrative_identity(self, path: Path):
        if path.name.casefold() in {"system volume information", "$recycle.bin"}:
            raise AssertionError("administrative entry must not enter the write plan")
        return original_observe(self, path)

    monkeypatch.setattr(Path, "rglob", reject_administrative_recursion)
    monkeypatch.setattr(WindowsPathIdentityAdapter, "observe", reject_administrative_identity)
    result = _materialize(_catalog(_entry(VirtualPath(("playlists", "one.nml")))), root)
    assert result.changed
    assert (root / "nml-catalog-manifest.json").is_file()
    for entry in administrative_entries:
        assert (entry / "untouched.txt").read_text() == "administrative"


@pytest.mark.parametrize("name", ["System Volume Information backup", "foreign", "foreign.nml"])
def test_similar_or_unknown_root_entries_remain_blocked(tmp_path: Path, name: str) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    entry = root / name
    if entry.suffix:
        entry.write_text("foreign")
    else:
        entry.mkdir()
    with pytest.raises(UnmanagedNmlCatalogContentError):
        _materialize(_catalog(_entry(VirtualPath(("playlists", "one.nml")))), root)


def test_managed_catalog_refresh_preserves_administrative_entries_and_blocks_unknown(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    old_path = VirtualPath(("playlists", "old.nml"))
    _materialize(_catalog(_entry(old_path)), root)
    administrative = root / "System Volume Information"
    administrative.mkdir()
    (administrative / "untouched.txt").write_text("administrative")

    new_path = VirtualPath(("playlists", "new.nml"))
    refreshed = _materialize(_catalog(_entry(new_path), revision="next"), root)
    assert refreshed.changed
    assert (administrative / "untouched.txt").read_text() == "administrative"
    assert not (root / "playlists" / "old.nml").exists()
    assert (root / "playlists" / "new.nml").exists()

    (root / "foreign.txt").write_text("foreign")
    with pytest.raises(UnmanagedNmlCatalogContentError):
        _materialize(_catalog(_entry(VirtualPath(("playlists", "last.nml"))), revision="last"), root)


def test_catalog_cannot_claim_administrative_root_entry(tmp_path: Path) -> None:
    catalog = _catalog(_entry(VirtualPath(("System Volume Information", "one.nml"))))
    with pytest.raises(NmlCatalogManifestError, match="administrative root entry"):
        _materialize(catalog, tmp_path / "catalog")


def test_symlink_at_catalog_root_remains_blocked(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    link = root / "foreign-link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable in this test environment")
    with pytest.raises(UnmanagedNmlCatalogContentError):
        _materialize(_catalog(_entry(VirtualPath(("playlists", "one.nml")))), root)


def test_rejects_escaped_paths_without_writing_outside_target(tmp_path: Path) -> None:
    escaped = VirtualPath(("playlists", "safe.nml"))
    object.__setattr__(escaped, "segments", ("..", "outside.nml"))
    catalog = _catalog(_entry(escaped))
    with pytest.raises(EscapingNmlCatalogPathError):
        _materialize(catalog, tmp_path / "catalog")
    assert not (tmp_path / "outside.nml").exists()


def test_rejects_source_mismatch_and_detectable_symlink_target(tmp_path: Path) -> None:
    catalog = _catalog(_entry(VirtualPath(("playlists", "one.nml"))))
    with pytest.raises(NmlCatalogSourceMismatchError):
        _materialize(catalog, tmp_path / "catalog", expected_source_id="other")

    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable in this test environment")
    with pytest.raises(InvalidNmlCatalogTargetError):
        _materialize(catalog, link / "catalog")


def test_materializer_has_no_vhd_usb_or_collection_dependencies() -> None:
    import rekordbox_usb.nml_catalog_materializer as materializer

    source = inspect.getsource(materializer)
    assert all(token not in source.lower() for token in ("vhd", "diskpart", "collection.nml", "win32"))
