"""Atomic local materialization of deterministic virtual NML catalogs."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from .source_write_guard import SourceWriteBoundary, WriteIntent, WritePlan
from .virtual_nml_catalog import NmlCatalog, NmlCatalogEntry
from .virtual_projection import VirtualPath


_MANIFEST_NAME = "nml-catalog-manifest.json"
_MANIFEST_SCHEMA_VERSION = 1
_ROOT_ADMINISTRATIVE_NAMES = frozenset({"system volume information", "$recycle.bin"})


class NmlCatalogMaterializationError(ValueError):
    """Base error for local NML catalog materialization."""


class InvalidNmlCatalogTargetError(NmlCatalogMaterializationError):
    """Raised when the explicit local target root is unsafe or invalid."""


class EscapingNmlCatalogPathError(NmlCatalogMaterializationError):
    """Raised when a logical catalog path cannot stay below the target root."""


class UnmanagedNmlCatalogContentError(NmlCatalogMaterializationError):
    """Raised when an existing root contains content outside the manifest."""


class NmlCatalogStagingError(NmlCatalogMaterializationError):
    """Raised when complete staged output cannot be constructed."""


class NmlCatalogHashError(NmlCatalogMaterializationError):
    """Raised when supplied or written NML content fails its SHA-256 check."""


class NmlCatalogPromotionError(NmlCatalogMaterializationError):
    """Raised when staged output cannot be promoted safely."""


class NmlCatalogManifestError(NmlCatalogMaterializationError):
    """Raised when an existing catalog manifest is missing or incompatible."""


class NmlCatalogSourceMismatchError(NmlCatalogMaterializationError):
    """Raised when a caller expects a different source identity."""


@dataclass(frozen=True, slots=True)
class NmlCatalogMaterializationResult:
    """Immutable result of one local catalog materialization attempt."""

    target_root: Path
    source_id: str
    snapshot_id: str
    written_paths: tuple[VirtualPath, ...]
    preserved_paths: tuple[VirtualPath, ...]
    removed_paths: tuple[VirtualPath, ...]
    content_hashes: tuple[tuple[VirtualPath, str], ...]
    changed: bool


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise NmlCatalogManifestError(f"{field_name} must be a non-empty string")
    return value


def _target_path(target_root: Path, logical_path: VirtualPath) -> Path:
    if not isinstance(logical_path, VirtualPath) or not logical_path.segments:
        raise EscapingNmlCatalogPathError("catalog entry requires a non-empty VirtualPath")
    if any(
        not isinstance(segment, str)
        or not segment
        or segment in {".", ".."}
        or "\x00" in segment
        or "/" in segment
        or "\\" in segment
        or ":" in segment
        for segment in logical_path.segments
    ):
        raise EscapingNmlCatalogPathError("catalog path contains an unsafe segment")
    candidate = target_root.joinpath(*logical_path.segments)
    try:
        candidate.relative_to(target_root)
    except ValueError as exc:
        raise EscapingNmlCatalogPathError(logical_path.value) from exc
    return candidate


def _validate_target_root(target_root: Path) -> None:
    if not isinstance(target_root, Path) or not target_root.is_absolute():
        raise InvalidNmlCatalogTargetError("target_root must be an explicit absolute Path")
    if target_root.exists() and not target_root.is_dir():
        raise InvalidNmlCatalogTargetError("target_root must be a directory")
    for current in (target_root, *target_root.parents):
        if current.exists() and current.is_symlink():
            raise InvalidNmlCatalogTargetError("target_root must not traverse a symlink")
    if not target_root.parent.exists() or not target_root.parent.is_dir():
        raise InvalidNmlCatalogTargetError("target_root parent must already exist")


def _entry_payload(entry: NmlCatalogEntry) -> dict[str, object]:
    return {
        "playlist_id": entry.playlist_id,
        "logical_path": entry.destination_path.value,
        "sha256": entry.content_sha256,
        "track_count": entry.track_count,
    }


def _manifest_payload(catalog: NmlCatalog) -> dict[str, object]:
    return {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "source_id": catalog.source_id,
        "snapshot_id": catalog.snapshot_id,
        "entries": [_entry_payload(entry) for entry in catalog.entries],
    }


def _manifest_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _checked(boundary: SourceWriteBoundary, plan: WritePlan, path: Path, action):
    permit = boundary.authorize(plan)
    try:
        permit.revalidate_before_side_effect(path)
        return action()
    finally:
        permit.close()


def _write_file(path: Path, content: bytes, boundary: SourceWriteBoundary, plan: WritePlan) -> None:
    missing: list[Path] = []
    current = path.parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        _checked(boundary, plan, directory, directory.mkdir)
    permit = boundary.authorize(plan)
    try:
        permit.revalidate_before_side_effect(path)
        with path.open("xb") as handle:
            permit.record_created(path)
            permit.revalidate_before_side_effect(path)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        permit.close()


def _read_manifest(target_root: Path) -> dict[str, object] | None:
    manifest_path = target_root / _MANIFEST_NAME
    if not manifest_path.exists():
        return None
    if manifest_path.is_symlink():
        raise NmlCatalogManifestError("manifest must not be a symlink")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NmlCatalogManifestError("manifest cannot be read") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
        raise NmlCatalogManifestError("manifest schema is incompatible")
    if not isinstance(payload.get("entries"), list):
        raise NmlCatalogManifestError("manifest entries are invalid")
    _require_text(payload.get("source_id"), "manifest source_id")
    _require_text(payload.get("snapshot_id"), "manifest snapshot_id")
    return payload


def _managed_paths(manifest: dict[str, object]) -> set[str]:
    entries = manifest["entries"]
    assert isinstance(entries, list)
    paths = {_MANIFEST_NAME}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("logical_path"), str):
            raise NmlCatalogManifestError("manifest entry is invalid")
        if entry["logical_path"].split("/", 1)[0].casefold() in _ROOT_ADMINISTRATIVE_NAMES:
            raise NmlCatalogManifestError("manifest must not manage an administrative root entry")
        paths.add(entry["logical_path"])
    return paths


def _is_administrative_root_entry(path: Path) -> bool:
    """Allow only exact known NTFS administrative names at the catalog root."""
    return path.name.casefold() in _ROOT_ADMINISTRATIVE_NAMES


def _root_entries(target_root: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return administrative and catalog-relevant direct children without recursion."""
    administrative: list[Path] = []
    relevant: list[Path] = []
    for item in target_root.iterdir():
        if item.is_symlink():
            raise UnmanagedNmlCatalogContentError("managed root must not contain symlinks")
        if _is_administrative_root_entry(item):
            administrative.append(item)
        else:
            relevant.append(item)
    return tuple(administrative), tuple(relevant)


def _catalog_contents(target_root: Path) -> set[str]:
    """Inspect only catalog-relevant roots; administrative entries are never traversed."""
    _, relevant_roots = _root_entries(target_root)
    contents: set[str] = set()
    for root in relevant_roots:
        contents.add(root.relative_to(target_root).as_posix())
        if root.is_dir():
            for item in root.rglob("*"):
                if item.is_symlink():
                    raise UnmanagedNmlCatalogContentError("managed root must not contain symlinks")
                contents.add(item.relative_to(target_root).as_posix())
    return contents


def _validate_existing_root(target_root: Path) -> dict[str, object] | None:
    if not target_root.exists():
        return None
    if target_root.is_symlink():
        raise InvalidNmlCatalogTargetError("target_root must not be a symlink")
    contents = _catalog_contents(target_root)
    if not contents:
        return None
    manifest = _read_manifest(target_root)
    if manifest is None:
        raise UnmanagedNmlCatalogContentError("non-empty target_root has no managed manifest")
    managed = _managed_paths(manifest)
    unknown = {path for path in contents if path not in managed and not any(value.startswith(path + "/") for value in managed)}
    if unknown:
        raise UnmanagedNmlCatalogContentError("target_root contains unmanaged content")
    return manifest


def _validate_catalog(catalog: NmlCatalog, expected_source_id: str | None) -> None:
    if not isinstance(catalog, NmlCatalog):
        raise TypeError("catalog must be an NmlCatalog")
    _require_text(catalog.source_id, "catalog source_id")
    _require_text(catalog.snapshot_id, "catalog snapshot_id")
    if expected_source_id is not None and catalog.source_id != expected_source_id:
        raise NmlCatalogSourceMismatchError(catalog.source_id)
    destinations: set[VirtualPath] = set()
    for entry in catalog.entries:
        if not isinstance(entry, NmlCatalogEntry):
            raise NmlCatalogManifestError("catalog entries must be NmlCatalogEntry values")
        if entry.source_id != catalog.source_id or entry.snapshot_id != catalog.snapshot_id:
            raise NmlCatalogManifestError("catalog entry identity does not match catalog")
        if entry.destination_path in destinations:
            raise NmlCatalogManifestError("catalog destination paths must be unique")
        if entry.destination_path.segments[0].casefold() in _ROOT_ADMINISTRATIVE_NAMES:
            raise NmlCatalogManifestError("catalog destination uses an administrative root entry")
        destinations.add(entry.destination_path)
        actual_hash = sha256(entry.content.encode("utf-8")).hexdigest()
        if actual_hash != entry.content_sha256:
            raise NmlCatalogHashError(entry.destination_path.value)


def _matches_existing(catalog: NmlCatalog, target_root: Path, manifest: dict[str, object]) -> bool:
    if manifest.get("source_id") != catalog.source_id or manifest.get("snapshot_id") != catalog.snapshot_id:
        return False
    expected = _manifest_payload(catalog)
    if manifest != expected:
        return False
    for entry in catalog.entries:
        path = _target_path(target_root, entry.destination_path)
        if not path.is_file() or path.is_symlink():
            return False
        if sha256(path.read_bytes()).hexdigest() != entry.content_sha256:
            return False
    return True


def _virtual_path(value: str) -> VirtualPath:
    return VirtualPath(tuple(value.split("/")))


def _is_volume_root(path: Path) -> bool:
    """Recognize a filesystem anchor without depending on the host platform."""
    return path.parent == path


def _temporary_roots(target_root: Path) -> tuple[Path, Path]:
    token = uuid4().hex
    if _is_volume_root(target_root):
        return (
            target_root / f".traktorbox-staging-{token}",
            target_root / f".traktorbox-backup-{token}",
        )
    return (
        target_root.parent / f".{target_root.name}.staging-{token}",
        target_root.parent / f".{target_root.name}.backup-{token}",
    )


def _promote_preserving_administrative_entries(
    target_root: Path, staging: Path, backup: Path,
    boundary: SourceWriteBoundary, plan: WritePlan,
) -> None:
    """Replace only validated catalog roots, leaving allowed NTFS entries in place."""
    _, relevant_roots = _root_entries(target_root)
    relevant_roots = tuple(item for item in relevant_roots if item not in {staging, backup})
    _checked(boundary, plan, backup, backup.mkdir)
    moved_existing: list[Path] = []
    moved_staging: list[Path] = []
    try:
        for item in relevant_roots:
            destination = backup / item.name
            _checked(boundary, plan, item, lambda: _checked(boundary, plan, destination, lambda: item.rename(destination)))
            moved_existing.append(destination)
        for item in staging.iterdir():
            destination = target_root / item.name
            _checked(boundary, plan, item, lambda: _checked(boundary, plan, destination, lambda: item.rename(destination)))
            moved_staging.append(destination)
    except BaseException:
        for item in reversed(moved_staging):
            if item.exists():
                destination = staging / item.name
                _checked(boundary, plan, item, lambda: _checked(boundary, plan, destination, lambda: item.rename(destination)))
        for item in reversed(moved_existing):
            if item.exists():
                destination = target_root / item.name
                _checked(boundary, plan, item, lambda: _checked(boundary, plan, destination, lambda: item.rename(destination)))
        raise


def materialize_nml_catalog(
    catalog: NmlCatalog,
    target_root: Path,
    *,
    expected_source_id: str | None = None,
    replace_existing: bool = True,
    source_root: Path | None = None,
) -> NmlCatalogMaterializationResult:
    """Materialize a complete catalog transactionally below one local root."""
    _validate_target_root(target_root)
    _validate_catalog(catalog, expected_source_id)
    # A source-derived catalog cannot be materialized without the actual mount.
    if source_root is None:
        from .source_write_guard import SourceWriteBoundaryError, WriteDenial
        raise SourceWriteBoundaryError(WriteDenial.SOURCE_IDENTITY_UNAVAILABLE)
    boundary = SourceWriteBoundary(source_root)
    existing_manifest = _validate_existing_root(target_root)
    hashes = tuple((entry.destination_path, entry.content_sha256) for entry in catalog.entries)
    if existing_manifest is not None and _matches_existing(catalog, target_root, existing_manifest):
        return NmlCatalogMaterializationResult(
            target_root,
            catalog.source_id,
            catalog.snapshot_id,
            (),
            tuple(entry.destination_path for entry in catalog.entries),
            (),
            hashes,
            False,
        )
    if existing_manifest is not None and not replace_existing:
        raise NmlCatalogPromotionError("target_root already contains a different catalog")

    staging, backup = _temporary_roots(target_root)
    plan_paths = {target_root, staging, backup}
    for root in (target_root, staging, backup):
        plan_paths.add(root / _MANIFEST_NAME)
    for entry in catalog.entries:
        for root in (target_root, staging, backup):
            path = _target_path(root, entry.destination_path)
            plan_paths.update((path, *path.parents))
    if target_root.exists():
        _, relevant_roots = _root_entries(target_root)
        for item in relevant_roots:
            plan_paths.update((item, backup / item.name))
    plan = WritePlan(WriteIntent.CATALOG_MATERIALIZE, tuple(plan_paths))
    boundary.authorize(plan).close()  # Entire plan before staging or backup effects.
    previous_paths = (
        {_virtual_path(entry["logical_path"]) for entry in existing_manifest["entries"]}
        if existing_manifest is not None
        else set()
    )
    next_paths = {entry.destination_path for entry in catalog.entries}
    try:
        _checked(boundary, plan, staging, staging.mkdir)
        for entry in catalog.entries:
            path = _target_path(staging, entry.destination_path)
            _write_file(path, entry.content.encode("utf-8"), boundary, plan)
            if sha256(path.read_bytes()).hexdigest() != entry.content_sha256:
                raise NmlCatalogHashError(entry.destination_path.value)
        _write_file(staging / _MANIFEST_NAME, _manifest_bytes(_manifest_payload(catalog)), boundary, plan)
    except BaseException as exc:
        if staging.exists():
            _checked(boundary, plan, staging, lambda: shutil.rmtree(staging, ignore_errors=True))
        if isinstance(exc, NmlCatalogMaterializationError):
            raise
        raise NmlCatalogStagingError("unable to construct complete staging catalog") from exc

    try:
        administrative_entries = _root_entries(target_root)[0] if target_root.exists() else ()
        if _is_volume_root(target_root) or administrative_entries:
            _promote_preserving_administrative_entries(target_root, staging, backup, boundary, plan)
        else:
            if target_root.exists():
                _checked(boundary, plan, target_root, lambda: _checked(boundary, plan, backup, lambda: target_root.rename(backup)))
            _checked(boundary, plan, staging, lambda: _checked(boundary, plan, target_root, lambda: staging.rename(target_root)))
    except BaseException as exc:
        if not target_root.exists() and backup.exists():
            _checked(boundary, plan, backup, lambda: _checked(boundary, plan, target_root, lambda: backup.rename(target_root)))
        if staging.exists():
            _checked(boundary, plan, staging, lambda: shutil.rmtree(staging, ignore_errors=True))
        raise NmlCatalogPromotionError("unable to promote complete staging catalog") from exc
    finally:
        if staging.exists():
            _checked(boundary, plan, staging, lambda: shutil.rmtree(staging, ignore_errors=True))
    if backup.exists():
        _checked(boundary, plan, backup, lambda: shutil.rmtree(backup))
    return NmlCatalogMaterializationResult(
        target_root,
        catalog.source_id,
        catalog.snapshot_id,
        tuple(entry.destination_path for entry in catalog.entries),
        (),
        tuple(sorted(previous_paths - next_paths, key=lambda value: value.value)),
        hashes,
        True,
    )
