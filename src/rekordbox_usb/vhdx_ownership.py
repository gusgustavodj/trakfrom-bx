"""Durable, fail-closed ownership capabilities for managed VHDX resources.

The ledger is a claim, not authority.  A destructive capability is issued only
when the durable generation and the currently observed file identity agree.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
import json
import os
from pathlib import Path
import threading
from typing import Iterator
from uuid import uuid4

from .source_write_guard import (
    CanonicalPathIdentity,
    LocalDestinationBinding,
    SourceWriteBoundary,
    WriteIntent,
    WritePlan,
    WindowsPathIdentityAdapter,
    atomic_write_bytes,
)


LEDGER_SCHEMA_VERSION = 1
LEDGER_FILENAME = ".traktorbox-vhdx-ownership.json"
LOCK_FILENAME = ".traktorbox-vhdx-ownership.lock"


class VhdxOwnershipState(StrEnum):
    OWNERSHIP_PENDING = "OWNERSHIP_PENDING"
    OWNED_VALID = "OWNED_VALID"
    MANAGED_DETACHED = "MANAGED_DETACHED"
    MANAGED_ATTACHED = "MANAGED_ATTACHED"
    EXPOSED = "EXPOSED"
    OWNED_STALE = "OWNED_STALE"
    FOREIGN = "FOREIGN"
    UNKNOWN = "UNKNOWN"
    MISSING = "MISSING"
    PARTIAL = "PARTIAL"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class VhdxOwnershipDenial(StrEnum):
    LEDGER_MISSING = "LEDGER_MISSING"
    LEDGER_INVALID = "LEDGER_INVALID"
    LEDGER_LOCKED = "LEDGER_LOCKED"
    PREEXISTING_UNKNOWN = "PREEXISTING_UNKNOWN"
    OWNERSHIP_REQUIRED = "P0_05_OWNERSHIP_REQUIRED"
    IDENTITY_MISSING = "IDENTITY_MISSING"
    IDENTITY_CHANGED = "IDENTITY_CHANGED"
    GENERATION_MISMATCH = "GENERATION_MISMATCH"
    OPERATION_MISMATCH = "OPERATION_MISMATCH"
    CAPABILITY_USED = "CAPABILITY_USED"
    STATE_INCOMPATIBLE = "STATE_INCOMPATIBLE"


class VhdxOwnershipError(ValueError):
    """A fail-closed ownership decision with a stable diagnostic code."""

    def __init__(self, code: VhdxOwnershipDenial, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code.value)


@dataclass(frozen=True, slots=True)
class VhdxFileIdentity:
    canonical_path: str
    volume_serial: int
    file_id: str

    @classmethod
    def observe(cls, path: Path) -> VhdxFileIdentity | None:
        observed = WindowsPathIdentityAdapter().observe(path)
        if observed is None:
            return None
        return cls(observed.final_path, observed.volume_serial, observed.file_id.hex())


@dataclass(frozen=True, slots=True)
class VhdxOwnershipRecord:
    path: Path
    source_id: str
    generation: str
    operation_id: str
    state: VhdxOwnershipState
    identity: VhdxFileIdentity | None
    write_binding: LocalDestinationBinding | None
    updated_at: str


@dataclass(slots=True)
class VhdxOwnershipCapability:
    """Single-use proof tied to one action, generation and physical file."""

    ledger: VhdxOwnershipLedger
    path: Path
    generation: str
    action: str
    identity: VhdxFileIdentity
    nonce: str
    consumed: bool = False

    def consume(self, current_identity: VhdxFileIdentity | None) -> None:
        self.ledger.consume(self, current_identity)

    def assert_consumed(
        self,
        *,
        action: str,
        path: Path,
        current_identity: VhdxFileIdentity | None,
    ) -> None:
        if not self.consumed:
            raise VhdxOwnershipError(VhdxOwnershipDenial.OWNERSHIP_REQUIRED)
        if action != self.action or path != self.path:
            raise VhdxOwnershipError(VhdxOwnershipDenial.OPERATION_MISMATCH)
        if current_identity != self.identity:
            raise VhdxOwnershipError(VhdxOwnershipDenial.IDENTITY_CHANGED)


_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.realpath(path))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


def _identity_payload(value: VhdxFileIdentity | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "canonical_path": value.canonical_path,
        "volume_serial": value.volume_serial,
        "file_id": value.file_id,
    }


def _identity_from_payload(value: object) -> VhdxFileIdentity | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
    canonical = value.get("canonical_path")
    serial = value.get("volume_serial")
    file_id = value.get("file_id")
    if not isinstance(canonical, str) or not isinstance(serial, int) or not isinstance(file_id, str):
        raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
    return VhdxFileIdentity(canonical, serial, file_id)


def _canonical_payload(value: CanonicalPathIdentity) -> dict[str, object]:
    return {
        "final_path": value.final_path,
        "volume_serial": value.volume_serial,
        "file_id": value.file_id.hex(),
    }


def _canonical_from_payload(value: object) -> CanonicalPathIdentity:
    if not isinstance(value, dict):
        raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
    final_path = value.get("final_path")
    serial = value.get("volume_serial")
    file_id = value.get("file_id")
    if not isinstance(final_path, str) or not isinstance(serial, int) or not isinstance(file_id, str):
        raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
    try:
        raw_id = bytes.fromhex(file_id)
    except ValueError as exc:
        raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID) from exc
    return CanonicalPathIdentity(final_path, serial, raw_id)


def _binding_payload(value: LocalDestinationBinding | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "source_root": str(value.source_root),
        "source_identity": _canonical_payload(value.source_identity),
        "source_parts": list(value.source_parts),
        "destination": str(value.destination),
        "parent_identity": _canonical_payload(value.parent_identity),
        "parent_parts": list(value.parent_parts),
        "intent": value.intent.value,
    }


def _binding_from_payload(value: object) -> LocalDestinationBinding | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
    source_root = value.get("source_root")
    destination = value.get("destination")
    source_parts = value.get("source_parts")
    parent_parts = value.get("parent_parts")
    intent = value.get("intent")
    if (
        not isinstance(source_root, str)
        or not isinstance(destination, str)
        or not isinstance(source_parts, list)
        or not all(isinstance(item, str) for item in source_parts)
        or not isinstance(parent_parts, list)
        or not all(isinstance(item, str) for item in parent_parts)
        or not isinstance(intent, str)
    ):
        raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
    try:
        write_intent = WriteIntent(intent)
    except ValueError as exc:
        raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID) from exc
    return LocalDestinationBinding(
        Path(source_root),
        _canonical_from_payload(value.get("source_identity")),
        tuple(source_parts),
        Path(destination),
        _canonical_from_payload(value.get("parent_identity")),
        tuple(parent_parts),
        write_intent,
    )


class VhdxOwnershipLedger:
    """Atomic ownership ledger with a process and OS level transaction lock."""

    def __init__(
        self,
        storage_root: Path,
        *,
        boundary: SourceWriteBoundary | None = None,
    ) -> None:
        if not storage_root.is_absolute():
            raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
        self.storage_root = storage_root
        self.path = storage_root / LEDGER_FILENAME
        self.lock_path = storage_root / LOCK_FILENAME
        self._boundary = boundary
        self._thread_lock = _process_lock(self.path)

    @staticmethod
    def observe(path: Path) -> VhdxFileIdentity | None:
        return VhdxFileIdentity.observe(path)

    def _empty_payload(self) -> dict[str, object]:
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "ledger_binding": None,
            "lock_binding": None,
            "records": {},
        }

    def _read_payload(self) -> dict[str, object]:
        if not self.path.is_file() or self.path.is_symlink():
            raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_MISSING)
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != LEDGER_SCHEMA_VERSION
            or not isinstance(value.get("records"), dict)
        ):
            raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
        return value

    def _initialize(self) -> None:
        boundary = self._boundary
        if boundary is None:
            raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_MISSING)
        if not self.storage_root.is_dir() or self.storage_root.is_symlink():
            raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
        created_lock = False
        permit = boundary.authorize(WritePlan(WriteIntent.STATE, (self.lock_path,)))
        try:
            permit.revalidate_before_side_effect(self.lock_path, intent=WriteIntent.STATE)
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            except FileExistsError:
                if self.path.exists():
                    return
                raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_LOCKED)
            else:
                created_lock = True
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(b"0")
                    handle.flush()
                    os.fsync(handle.fileno())
                permit.record_created(self.lock_path)
        finally:
            permit.close()
        if not created_lock:
            raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_LOCKED)
        ledger_binding = boundary.capture_local_destination(self.path, intent=WriteIntent.STATE)
        lock_binding = boundary.capture_local_destination(self.lock_path, intent=WriteIntent.STATE)
        payload = self._empty_payload()
        payload["ledger_binding"] = _binding_payload(ledger_binding)
        payload["lock_binding"] = _binding_payload(lock_binding)
        self._write_payload(payload, binding=ledger_binding)

    @contextmanager
    def _locked_payload(self) -> Iterator[dict[str, object]]:
        with self._thread_lock:
            if not self.path.exists():
                self._initialize()
            self._read_payload()
            if not self.lock_path.is_file() or self.lock_path.is_symlink():
                raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
            with self.lock_path.open("r+b") as lock_handle:
                try:
                    if os.name == "nt":
                        import msvcrt

                        lock_handle.seek(0)
                        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                except OSError as exc:
                    raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_LOCKED) from exc
                try:
                    yield self._read_payload()
                finally:
                    if os.name == "nt":
                        lock_handle.seek(0)
                        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _write_payload(
        self,
        payload: dict[str, object],
        *,
        binding: LocalDestinationBinding | None = None,
    ) -> None:
        active_binding = binding or _binding_from_payload(payload.get("ledger_binding"))
        if self._boundary is not None:
            boundary = self._boundary
        elif active_binding is not None:
            boundary = SourceWriteBoundary.from_local_binding(active_binding)
        else:
            raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        atomic_write_bytes(boundary, self.path, encoded, intent=WriteIntent.STATE)

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(os.path.realpath(path))

    @staticmethod
    def _record_payload(record: VhdxOwnershipRecord) -> dict[str, object]:
        return {
            "path": str(record.path),
            "source_id": record.source_id,
            "generation": record.generation,
            "operation_id": record.operation_id,
            "state": record.state.value,
            "identity": _identity_payload(record.identity),
            "write_binding": _binding_payload(record.write_binding),
            "updated_at": record.updated_at,
        }

    @staticmethod
    def _record_from_payload(value: object) -> VhdxOwnershipRecord:
        if not isinstance(value, dict):
            raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
        required = ("path", "source_id", "generation", "operation_id", "state", "updated_at")
        if not all(isinstance(value.get(key), str) for key in required):
            raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
        try:
            state = VhdxOwnershipState(str(value["state"]))
        except ValueError as exc:
            raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID) from exc
        return VhdxOwnershipRecord(
            Path(str(value["path"])),
            str(value["source_id"]),
            str(value["generation"]),
            str(value["operation_id"]),
            state,
            _identity_from_payload(value.get("identity")),
            _binding_from_payload(value.get("write_binding")),
            str(value["updated_at"]),
        )

    def _get(self, payload: dict[str, object], path: Path) -> VhdxOwnershipRecord | None:
        records = payload["records"]
        assert isinstance(records, dict)
        raw = records.get(self._key(path))
        return None if raw is None else self._record_from_payload(raw)

    def _put(self, payload: dict[str, object], record: VhdxOwnershipRecord) -> None:
        records = payload["records"]
        assert isinstance(records, dict)
        records[self._key(record.path)] = self._record_payload(record)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def begin_create(
        self,
        path: Path,
        source_id: str,
        current_identity: VhdxFileIdentity | None = None,
    ) -> VhdxOwnershipRecord:
        """Reserve a new generation only while the target is physically absent."""
        if current_identity is not None:
            raise VhdxOwnershipError(VhdxOwnershipDenial.PREEXISTING_UNKNOWN)
        with self._locked_payload() as payload:
            existing = self._get(payload, path)
            if existing is not None and existing.state not in {VhdxOwnershipState.MISSING}:
                raise VhdxOwnershipError(VhdxOwnershipDenial.STATE_INCOMPATIBLE)
            record = VhdxOwnershipRecord(
                path,
                source_id,
                uuid4().hex,
                uuid4().hex,
                VhdxOwnershipState.OWNERSHIP_PENDING,
                None,
                None,
                self._now(),
            )
            self._put(payload, record)
            self._write_payload(payload)
            return record

    def complete_create(
        self,
        pending: VhdxOwnershipRecord,
        identity: VhdxFileIdentity | None,
    ) -> VhdxOwnershipRecord:
        if identity is None or self._boundary is None:
            raise VhdxOwnershipError(VhdxOwnershipDenial.IDENTITY_MISSING)
        with self._locked_payload() as payload:
            current = self._get(payload, pending.path)
            if current is None or current.generation != pending.generation:
                raise VhdxOwnershipError(VhdxOwnershipDenial.GENERATION_MISMATCH)
            if current.state is not VhdxOwnershipState.OWNERSHIP_PENDING:
                raise VhdxOwnershipError(VhdxOwnershipDenial.STATE_INCOMPATIBLE)
            binding = self._boundary.capture_local_destination(
                pending.path, intent=WriteIntent.VHDX
            )
            completed = replace(
                current,
                state=VhdxOwnershipState.OWNED_VALID,
                identity=identity,
                write_binding=binding,
                updated_at=self._now(),
            )
            self._put(payload, completed)
            self._write_payload(payload)
            return completed

    def mark_partial(
        self,
        pending: VhdxOwnershipRecord,
        identity: VhdxFileIdentity | None,
    ) -> VhdxOwnershipRecord:
        with self._locked_payload() as payload:
            current = self._get(payload, pending.path)
            if current is None or current.generation != pending.generation:
                raise VhdxOwnershipError(VhdxOwnershipDenial.GENERATION_MISMATCH)
            binding = None
            if identity is not None and self._boundary is not None:
                binding = self._boundary.capture_local_destination(
                    pending.path, intent=WriteIntent.VHDX
                )
            partial = replace(
                current,
                state=VhdxOwnershipState.PARTIAL,
                identity=identity,
                write_binding=binding,
                updated_at=self._now(),
            )
            self._put(payload, partial)
            self._write_payload(payload)
            return partial

    def mark_missing(self, pending: VhdxOwnershipRecord) -> VhdxOwnershipRecord:
        """Close an absent failed-create generation so an explicit retry can begin."""
        with self._locked_payload() as payload:
            current = self._get(payload, pending.path)
            if (
                current is None
                or current.generation != pending.generation
                or current.operation_id != pending.operation_id
            ):
                raise VhdxOwnershipError(VhdxOwnershipDenial.GENERATION_MISMATCH)
            if current.state not in {
                VhdxOwnershipState.OWNERSHIP_PENDING,
                VhdxOwnershipState.PARTIAL,
            }:
                raise VhdxOwnershipError(VhdxOwnershipDenial.STATE_INCOMPATIBLE)
            missing = replace(
                current,
                state=VhdxOwnershipState.MISSING,
                identity=None,
                write_binding=None,
                updated_at=self._now(),
            )
            self._put(payload, missing)
            self._write_payload(payload)
            return missing

    def status(
        self,
        path: Path,
        current_identity: VhdxFileIdentity | None,
    ) -> VhdxOwnershipState:
        try:
            with self._locked_payload() as payload:
                record = self._get(payload, path)
        except VhdxOwnershipError as exc:
            if exc.code is VhdxOwnershipDenial.LEDGER_MISSING:
                return VhdxOwnershipState.UNKNOWN
            raise
        if record is None:
            return VhdxOwnershipState.FOREIGN if current_identity is not None else VhdxOwnershipState.UNKNOWN
        if current_identity is None:
            return VhdxOwnershipState.MISSING
        if record.identity != current_identity:
            return VhdxOwnershipState.OWNED_STALE
        return record.state

    def record(self, path: Path) -> VhdxOwnershipRecord | None:
        """Read an existing ownership record without creating ledger state."""
        if not self.path.is_file():
            return None
        with self._locked_payload() as payload:
            return self._get(payload, path)

    def records(self) -> tuple[VhdxOwnershipRecord, ...]:
        """List existing claims without initializing or changing the ledger."""
        if not self.path.exists():
            return ()
        with self._locked_payload() as payload:
            raw = payload["records"]
            assert isinstance(raw, dict)
            records = tuple(self._record_from_payload(value) for value in raw.values())
            if any(self._key(record.path) != key for key, record in zip(raw, records)):
                raise VhdxOwnershipError(VhdxOwnershipDenial.LEDGER_INVALID)
            return records

    def authorize(
        self,
        path: Path,
        current_identity: VhdxFileIdentity | None,
        action: str,
        *,
        expected_generation: str | None = None,
        operation_id: str | None = None,
        allow_partial: bool = False,
    ) -> VhdxOwnershipCapability:
        if current_identity is None:
            raise VhdxOwnershipError(VhdxOwnershipDenial.IDENTITY_MISSING)
        with self._locked_payload() as payload:
            record = self._get(payload, path)
            if record is None:
                raise VhdxOwnershipError(VhdxOwnershipDenial.OWNERSHIP_REQUIRED)
            if expected_generation is not None and record.generation != expected_generation:
                raise VhdxOwnershipError(VhdxOwnershipDenial.GENERATION_MISMATCH)
            if operation_id is not None and record.operation_id != operation_id:
                raise VhdxOwnershipError(VhdxOwnershipDenial.OPERATION_MISMATCH)
            allowed = {
                VhdxOwnershipState.OWNED_VALID,
                VhdxOwnershipState.MANAGED_DETACHED,
                VhdxOwnershipState.MANAGED_ATTACHED,
                VhdxOwnershipState.EXPOSED,
            }
            if allow_partial:
                allowed.add(VhdxOwnershipState.PARTIAL)
            if record.state not in allowed:
                raise VhdxOwnershipError(VhdxOwnershipDenial.STATE_INCOMPATIBLE)
            if record.identity != current_identity:
                raise VhdxOwnershipError(VhdxOwnershipDenial.IDENTITY_CHANGED)
            return VhdxOwnershipCapability(
                self,
                path,
                record.generation,
                action,
                current_identity,
                uuid4().hex,
            )

    def consume(
        self,
        capability: VhdxOwnershipCapability,
        current_identity: VhdxFileIdentity | None,
    ) -> None:
        if capability.ledger is not self or capability.consumed:
            raise VhdxOwnershipError(VhdxOwnershipDenial.CAPABILITY_USED)
        with self._locked_payload() as payload:
            record = self._get(payload, capability.path)
            if record is None or record.generation != capability.generation:
                raise VhdxOwnershipError(VhdxOwnershipDenial.GENERATION_MISMATCH)
            if record.identity != current_identity or current_identity != capability.identity:
                raise VhdxOwnershipError(VhdxOwnershipDenial.IDENTITY_CHANGED)
            capability.consumed = True

    def record_effect(
        self,
        capability: VhdxOwnershipCapability,
        state: VhdxOwnershipState,
    ) -> None:
        if not capability.consumed:
            raise VhdxOwnershipError(VhdxOwnershipDenial.OWNERSHIP_REQUIRED)
        with self._locked_payload() as payload:
            record = self._get(payload, capability.path)
            if record is None or record.generation != capability.generation:
                raise VhdxOwnershipError(VhdxOwnershipDenial.GENERATION_MISMATCH)
            updated = replace(
                record,
                state=state,
                identity=(None if state is VhdxOwnershipState.MISSING else record.identity),
                updated_at=self._now(),
            )
            self._put(payload, updated)
            self._write_payload(payload)

    def boundary_for(self, path: Path) -> SourceWriteBoundary:
        """Return the captured P0-04 boundary for an already-owned VHDX."""
        with self._locked_payload() as payload:
            record = self._get(payload, path)
            if record is None or record.write_binding is None:
                raise VhdxOwnershipError(VhdxOwnershipDenial.OWNERSHIP_REQUIRED)
            return SourceWriteBoundary.from_local_binding(record.write_binding)
