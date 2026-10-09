"""Authorial, fail-closed boundary for writes derived from removable sources.

The Windows adapter observes existing objects through read-only handles.  The
policy itself is independent of Win32 so synthetic adapters can exercise it.
This is an architectural capability, not a defense against hostile Python.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import struct
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4


class WriteDenial(StrEnum):
    SOURCE_IDENTITY_UNAVAILABLE = "SOURCE_IDENTITY_UNAVAILABLE"
    RELATIVE_DESTINATION = "RELATIVE_DESTINATION"
    INVALID_DESTINATION = "INVALID_DESTINATION"
    DESTINATION_EQUALS_SOURCE = "DESTINATION_EQUALS_SOURCE"
    DESTINATION_WITHIN_SOURCE = "DESTINATION_WITHIN_SOURCE"
    PROTECTED_COLLECTION_NML = "PROTECTED_COLLECTION_NML"
    REPARSE_IDENTITY_UNSAFE = "REPARSE_IDENTITY_UNSAFE"
    PATH_IDENTITY_CHANGED = "PATH_IDENTITY_CHANGED"
    TARGET_NOT_IN_WRITE_PLAN = "TARGET_NOT_IN_WRITE_PLAN"
    PERMIT_MISMATCH = "PERMIT_MISMATCH"


class SourceWriteBoundaryError(ValueError):
    def __init__(self, code: WriteDenial):
        self.code = code
        super().__init__(code.value)


class WriteIntent(StrEnum):
    NML_EXPORT = "nml_export"
    CATALOG_MATERIALIZE = "catalog_materialize"
    STATE = "state"
    CACHE = "cache"
    VHDX = "vhdx"
    TEMP = "temp"
    BACKUP = "backup"


@dataclass(frozen=True, slots=True)
class CanonicalPathIdentity:
    final_path: str
    volume_serial: int
    file_id: bytes


class PathIdentityAdapter(Protocol):
    def observe(self, path: Path) -> CanonicalPathIdentity | None:
        """Return identity for an existing path, None only if it does not exist.

        Ambiguous or inaccessible reparse points must raise rather than return
        None.  Implementations must not create or modify the path.
        """


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("volume_serial", ctypes.c_ulonglong), ("file_id", ctypes.c_ubyte * 16)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class WindowsPathIdentityAdapter:
    """Use Win32 handles for physical identity; reject reparse traversal."""

    _METADATA_QUERY_ACCESS = 0
    _FILE_SHARE_ALL = 0x7
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_ID_INFO_CLASS = 18
    _VOLUME_NAME_NT = 2
    _FILE_ID_UNSUPPORTED_ERRORS = frozenset((1, 50, 87))

    def __init__(self) -> None:
        if os.name != "nt":
            raise SourceWriteBoundaryError(WriteDenial.SOURCE_IDENTITY_UNAVAILABLE)
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self._kernel.CreateFileW.restype = wintypes.HANDLE
        self._kernel.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self._kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self._kernel.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._kernel.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ]
        self._kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL

    def _identity_for_handle(self, handle: int) -> tuple[int, bytes]:
        file_info = _FileIdInfo()
        if self._kernel.GetFileInformationByHandleEx(
            handle,
            self._FILE_ID_INFO_CLASS,
            ctypes.byref(file_info),
            ctypes.sizeof(file_info),
        ):
            return file_info.volume_serial, bytes(file_info.file_id)
        modern_error = ctypes.get_last_error()
        if modern_error not in self._FILE_ID_UNSUPPORTED_ERRORS:
            raise SourceWriteBoundaryError(WriteDenial.REPARSE_IDENTITY_UNSAFE)
        legacy = _ByHandleFileInformation()
        if not self._kernel.GetFileInformationByHandle(handle, ctypes.byref(legacy)):
            raise SourceWriteBoundaryError(WriteDenial.REPARSE_IDENTITY_UNSAFE)
        file_id = b"BY_HANDLE_FILE_INFORMATION\x00" + struct.pack(
            "<II", legacy.file_index_high, legacy.file_index_low
        )
        return legacy.volume_serial, file_id

    def observe(self, path: Path) -> CanonicalPathIdentity | None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SourceWriteBoundaryError(WriteDenial.REPARSE_IDENTITY_UNSAFE) from exc
        if info.st_file_attributes & self._FILE_ATTRIBUTE_REPARSE_POINT:
            raise SourceWriteBoundaryError(WriteDenial.REPARSE_IDENTITY_UNSAFE)
        handle = self._kernel.CreateFileW(
            str(path), self._METADATA_QUERY_ACCESS, self._FILE_SHARE_ALL, None,
            self._OPEN_EXISTING, self._FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise SourceWriteBoundaryError(
                WriteDenial.REPARSE_IDENTITY_UNSAFE
            ) from ctypes.WinError(ctypes.get_last_error())
        try:
            volume_serial, file_id = self._identity_for_handle(handle)
            size = 32768
            buffer = ctypes.create_unicode_buffer(size)
            count = self._kernel.GetFinalPathNameByHandleW(
                handle, buffer, size, self._VOLUME_NAME_NT
            )
            if not count or count >= size:
                raise SourceWriteBoundaryError(WriteDenial.REPARSE_IDENTITY_UNSAFE)
            return CanonicalPathIdentity(
                buffer.value, volume_serial, file_id
            )
        finally:
            self._kernel.CloseHandle(handle)


@dataclass(frozen=True, slots=True)
class WritePlan:
    intent: WriteIntent
    targets: tuple[Path, ...]

    def __post_init__(self) -> None:
        if not self.targets:
            raise SourceWriteBoundaryError(WriteDenial.INVALID_DESTINATION)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    lexical_path: Path
    ancestor: Path
    ancestor_identity: CanonicalPathIdentity
    projected_parts: tuple[str, ...]
    object_identity: CanonicalPathIdentity | None


@dataclass(frozen=True, slots=True)
class LocalDestinationBinding:
    """One local destination proved safe while its removable source was present."""

    source_root: Path
    source_identity: CanonicalPathIdentity
    source_parts: tuple[str, ...]
    destination: Path
    parent_identity: CanonicalPathIdentity
    parent_parts: tuple[str, ...]
    intent: WriteIntent = WriteIntent.STATE


def _parts(value: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in value.replace("/", "\\").split("\\") if part)


def _same_object(a: CanonicalPathIdentity, b: CanonicalPathIdentity) -> bool:
    return (a.volume_serial, a.file_id) == (b.volume_serial, b.file_id)


def _within(child: tuple[str, ...], parent: tuple[str, ...]) -> bool:
    return len(child) >= len(parent) and child[: len(parent)] == parent


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class SourceWriteBoundary:
    def __init__(
        self,
        source_root: Path,
        *,
        adapter: PathIdentityAdapter | None = None,
        approved_destination_base: Path | None = None,
        protected_collection_paths: tuple[Path, ...] = (),
    ) -> None:
        self._adapter = adapter or WindowsPathIdentityAdapter()
        self._base = approved_destination_base
        self.source_root = self._absolute(source_root, allow_relative=False)
        source = self._adapter.observe(self.source_root)
        if source is None:
            raise SourceWriteBoundaryError(WriteDenial.SOURCE_IDENTITY_UNAVAILABLE)
        self._source_identity = source
        self._source_parts = _parts(source.final_path)
        self._local_binding: LocalDestinationBinding | None = None
        self._protected_ids: set[tuple[int, bytes]] = set()
        for protected in protected_collection_paths:
            path = self._absolute(protected, allow_relative=False)
            observed = self._adapter.observe(path)
            if observed is not None:
                self._protected_ids.add((observed.volume_serial, observed.file_id))

    def _absolute(self, value: Path, *, allow_relative: bool) -> Path:
        raw = os.fspath(value)
        drive, tail = ntpath.splitdrive(raw)
        if not ntpath.isabs(raw) or (not drive and not raw.startswith("\\\\")):
            if not allow_relative or self._base is None or drive:
                raise SourceWriteBoundaryError(WriteDenial.RELATIVE_DESTINATION)
            base = os.fspath(self._base)
            if not ntpath.isabs(base):
                raise SourceWriteBoundaryError(WriteDenial.RELATIVE_DESTINATION)
            raw = ntpath.join(base, raw)
            drive, tail = ntpath.splitdrive(raw)
        if not drive or not ntpath.isabs(raw):
            raise SourceWriteBoundaryError(WriteDenial.RELATIVE_DESTINATION)
        segments = [part for part in tail.replace("/", "\\").split("\\") if part]
        for part in segments:
            stem = part.split(".", 1)[0].upper()
            if (
                part in {".", ".."} or part.endswith((" ", "."))
                or ":" in part or "\x00" in part or stem in _RESERVED
            ):
                raise SourceWriteBoundaryError(WriteDenial.INVALID_DESTINATION)
        return Path(ntpath.normpath(raw))

    def _snapshot(self, path: Path) -> _Snapshot:
        remaining: list[str] = []
        current = path
        while True:
            observed = self._adapter.observe(current)
            if observed is not None:
                break
            if current.parent == current:
                raise SourceWriteBoundaryError(WriteDenial.REPARSE_IDENTITY_UNSAFE)
            remaining.insert(0, current.name)
            current = current.parent
        projected = _parts(observed.final_path) + tuple(part.casefold() for part in remaining)
        return _Snapshot(path, current, observed, projected, observed if not remaining else None)

    def _assert_safe(self, snapshot: _Snapshot) -> None:
        if (
            snapshot.lexical_path.name.casefold() == "collection.nml"
            or snapshot.projected_parts[-1:] == ("collection.nml",)
            or (
                snapshot.object_identity is not None
                and (snapshot.object_identity.volume_serial, snapshot.object_identity.file_id)
                in self._protected_ids
            )
        ):
            raise SourceWriteBoundaryError(WriteDenial.PROTECTED_COLLECTION_NML)
        if snapshot.object_identity is not None and _same_object(
            snapshot.object_identity, self._source_identity
        ):
            raise SourceWriteBoundaryError(WriteDenial.DESTINATION_EQUALS_SOURCE)
        if _within(snapshot.projected_parts, self._source_parts):
            code = (
                WriteDenial.DESTINATION_EQUALS_SOURCE
                if snapshot.projected_parts == self._source_parts
                else WriteDenial.DESTINATION_WITHIN_SOURCE
            )
            raise SourceWriteBoundaryError(code)

    def authorize(self, plan: WritePlan) -> WritePermit:
        current_source = self._adapter.observe(self.source_root)
        if current_source is None:
            if self._local_binding is None:
                raise SourceWriteBoundaryError(WriteDenial.SOURCE_IDENTITY_UNAVAILABLE)
        elif not _same_object(current_source, self._source_identity):
            raise SourceWriteBoundaryError(WriteDenial.PATH_IDENTITY_CHANGED)
        elif _parts(current_source.final_path) != self._source_parts:
            raise SourceWriteBoundaryError(WriteDenial.PATH_IDENTITY_CHANGED)
        targets = {self._absolute(path, allow_relative=True) for path in plan.targets}
        if self._local_binding is not None:
            self._check_local_binding(targets, plan.intent)
        # Missing parents are plan members even if the caller omitted them.
        for target in tuple(targets):
            parent = target.parent
            while parent != parent.parent and self._adapter.observe(parent) is None:
                targets.add(parent)
                parent = parent.parent
        snapshots: dict[str, _Snapshot] = {}
        for target in sorted(targets, key=lambda p: (len(p.parts), str(p).casefold())):
            snapshot = self._snapshot(target)
            self._assert_safe(snapshot)
            snapshots[str(target).casefold()] = snapshot
        return WritePermit(self, plan.intent, snapshots)

    def capture_local_destination(
        self,
        destination: Path,
        *,
        intent: WriteIntent = WriteIntent.STATE,
    ) -> LocalDestinationBinding:
        """Bind one local destination for the same intent after source removal."""
        path = self._absolute(destination, allow_relative=False)
        permit = self.authorize(WritePlan(intent, (path,)))
        permit.close()
        parent = self._adapter.observe(path.parent)
        if parent is None:
            raise SourceWriteBoundaryError(WriteDenial.PATH_IDENTITY_CHANGED)
        parent_parts = _parts(parent.final_path)
        if _within(parent_parts, self._source_parts):
            raise SourceWriteBoundaryError(WriteDenial.DESTINATION_WITHIN_SOURCE)
        return LocalDestinationBinding(
            self.source_root, self._source_identity, self._source_parts,
            path, parent, parent_parts, intent,
        )

    @classmethod
    def from_local_binding(
        cls, binding: LocalDestinationBinding, *, adapter: PathIdentityAdapter | None = None
    ) -> SourceWriteBoundary:
        """Reauthorize only a captured local file after source disappearance."""
        boundary = object.__new__(cls)
        boundary._adapter = adapter or WindowsPathIdentityAdapter()
        boundary._base = None
        boundary.source_root = binding.source_root
        boundary._source_identity = binding.source_identity
        boundary._source_parts = binding.source_parts
        boundary._protected_ids = set()
        boundary._local_binding = binding
        return boundary

    def _check_local_binding(self, targets: set[Path], intent: WriteIntent) -> None:
        binding = self._local_binding
        assert binding is not None
        if intent is not binding.intent or binding.destination not in targets:
            raise SourceWriteBoundaryError(WriteDenial.TARGET_NOT_IN_WRITE_PLAN)
        parent = self._adapter.observe(binding.destination.parent)
        if (
            parent is None or not _same_object(parent, binding.parent_identity)
            or _parts(parent.final_path) != binding.parent_parts
        ):
            raise SourceWriteBoundaryError(WriteDenial.PATH_IDENTITY_CHANGED)
        prefix = f".{binding.destination.name}.".casefold()
        for target in targets:
            if target == binding.destination:
                continue
            name = target.name.casefold()
            if (
                target.parent != binding.destination.parent
                or not name.startswith(prefix) or not name.endswith(".tmp")
                or len(name[len(prefix):-4]) != 32
                or any(ch not in "0123456789abcdef" for ch in name[len(prefix):-4])
            ):
                raise SourceWriteBoundaryError(WriteDenial.TARGET_NOT_IN_WRITE_PLAN)


class WritePermit:
    def __init__(
        self, boundary: SourceWriteBoundary, intent: WriteIntent, snapshots: dict[str, _Snapshot]
    ) -> None:
        self._boundary = boundary
        self.intent = intent
        self._snapshots = snapshots
        self.nonce = uuid4().hex
        self._active = True

    def require(self, boundary: SourceWriteBoundary, target: Path, intent: WriteIntent) -> None:
        """Validate an exact target and the issuing boundary before a backend effect."""
        if self._boundary is not boundary:
            raise SourceWriteBoundaryError(WriteDenial.PERMIT_MISMATCH)
        self.revalidate_before_side_effect(target, intent=intent)

    def revalidate_before_side_effect(self, target: Path, *, intent: WriteIntent | None = None) -> None:
        if not self._active or (intent is not None and intent is not self.intent):
            raise SourceWriteBoundaryError(WriteDenial.PERMIT_MISMATCH)
        path = self._boundary._absolute(target, allow_relative=True)
        previous = self._snapshots.get(str(path).casefold())
        if previous is None:
            raise SourceWriteBoundaryError(WriteDenial.TARGET_NOT_IN_WRITE_PLAN)
        source = self._boundary._adapter.observe(self._boundary.source_root)
        if source is None and self._boundary._local_binding is None:
            raise SourceWriteBoundaryError(WriteDenial.PATH_IDENTITY_CHANGED)
        if source is not None and not _same_object(source, self._boundary._source_identity):
            raise SourceWriteBoundaryError(WriteDenial.PATH_IDENTITY_CHANGED)
        if source is not None and _parts(source.final_path) != self._boundary._source_parts:
            raise SourceWriteBoundaryError(WriteDenial.PATH_IDENTITY_CHANGED)
        if self._boundary._local_binding is not None:
            self._boundary._check_local_binding(
                {self._boundary._local_binding.destination}, self.intent
            )
        current = self._boundary._snapshot(path)
        self._boundary._assert_safe(current)
        if (
            current.projected_parts != previous.projected_parts
            or current.ancestor != previous.ancestor
            or not _same_object(current.ancestor_identity, previous.ancestor_identity)
            or (current.object_identity is None) != (previous.object_identity is None)
            or (
                current.object_identity is not None
                and previous.object_identity is not None
                and not _same_object(current.object_identity, previous.object_identity)
            )
        ):
            raise SourceWriteBoundaryError(WriteDenial.PATH_IDENTITY_CHANGED)

    def record_created(self, target: Path) -> None:
        """After an expected create, bind every affected projection to new IDs."""
        path = self._boundary._absolute(target, allow_relative=True)
        key = str(path).casefold()
        if not self._active or key not in self._snapshots:
            raise SourceWriteBoundaryError(WriteDenial.TARGET_NOT_IN_WRITE_PLAN)
        created = self._boundary._snapshot(path)
        self._boundary._assert_safe(created)
        if created.object_identity is None:
            raise SourceWriteBoundaryError(WriteDenial.PATH_IDENTITY_CHANGED)
        old = self._snapshots[key]
        if created.projected_parts != old.projected_parts:
            raise SourceWriteBoundaryError(WriteDenial.PATH_IDENTITY_CHANGED)
        for member_key, prior in tuple(self._snapshots.items()):
            if _within(prior.projected_parts, created.projected_parts):
                refreshed = self._boundary._snapshot(prior.lexical_path)
                self._boundary._assert_safe(refreshed)
                if refreshed.projected_parts != prior.projected_parts:
                    raise SourceWriteBoundaryError(WriteDenial.PATH_IDENTITY_CHANGED)
                self._snapshots[member_key] = refreshed

    def close(self) -> None:
        self._active = False


def _create_planned_parents(parent: Path, permit: WritePermit) -> None:
    missing: list[Path] = []
    current = parent
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            raise SourceWriteBoundaryError(WriteDenial.REPARSE_IDENTITY_UNSAFE)
        current = current.parent
    for directory in reversed(missing):
        permit.revalidate_before_side_effect(directory)
        directory.mkdir()
        permit.record_created(directory)


def atomic_write_bytes(
    boundary: SourceWriteBoundary,
    path: Path,
    payload: bytes,
    *,
    intent: WriteIntent,
) -> None:
    """Authorize final/temp/parents before the first mkdir or file create."""
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    permit = boundary.authorize(WritePlan(intent, (path, temp)))
    owned_temp = False
    try:
        _create_planned_parents(path.parent, permit)
        permit.revalidate_before_side_effect(temp, intent=intent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temp, flags, 0o600)
        owned_temp = True
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        permit.record_created(temp)
        permit.revalidate_before_side_effect(path, intent=intent)
        permit.revalidate_before_side_effect(temp, intent=intent)
        os.replace(temp, path)
        owned_temp = False
        permit.record_created(path)
    finally:
        if owned_temp:
            try:
                permit.revalidate_before_side_effect(temp, intent=intent)
            except SourceWriteBoundaryError:
                pass
            else:
                temp.unlink(missing_ok=True)
        permit.close()
