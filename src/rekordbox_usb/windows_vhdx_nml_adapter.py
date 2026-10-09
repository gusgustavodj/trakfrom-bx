"""Explicit, guarded Windows VHDX materialization for virtual NML catalogs.

This module deliberately has no process runner.  A platform integration supplies
the small backend protocol; unit tests use the same boundary with a fake backend.
No backend operation accepts a physical-disk identifier.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import time
from typing import Callable, Protocol, runtime_checkable

from .in_memory_virtual_projection import ProjectionOperationResult
from .nml_catalog_materializer import (
    NmlCatalogMaterializationError,
    NmlCatalogMaterializationResult,
    materialize_nml_catalog,
)
from .source_write_guard import SourceWriteBoundary, SourceWriteBoundaryError, WriteDenial, WriteIntent, WritePlan
from .vhdx_ownership import (
    VhdxFileIdentity,
    VhdxOwnershipCapability,
    VhdxOwnershipError,
    VhdxOwnershipLedger,
    VhdxOwnershipRecord,
    VhdxOwnershipState,
)
from .virtual_nml_catalog import NmlCatalog
from .virtual_projection import (
    ProjectionCapabilities,
    ProjectionInvalidatedError,
    SourceAlreadyPublishedError,
    SourceNotPublishedError,
)


class WindowsVhdxNmlVolumeError(ValueError):
    """Base error for the managed VHDX catalog adapter."""

    def __init__(
        self,
        message: str,
        *,
        lifecycle_stage: str | None = None,
        backend_operation: str | None = None,
        underlying_error: str | None = None,
        cause: Exception | None = None,
        vhdx_path: Path | None = None,
        created_before_failure: bool | None = None,
        attached_before_failure: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.lifecycle_stage = lifecycle_stage
        self.backend_operation = backend_operation
        self.underlying_error = underlying_error or (
            type(cause).__name__ if cause is not None else None
        )
        self.cause_type = type(cause).__name__ if cause is not None else None
        self.cause_message = str(cause) if cause is not None else None
        self.cause = cause
        self.vhdx_path = vhdx_path
        self.created_before_failure = created_before_failure
        self.attached_before_failure = attached_before_failure

    def diagnostic(self) -> dict[str, str | bool | None]:
        """Return safe, structured context for the publish-usb error boundary."""
        return {
            "error": type(self).__name__,
            "underlying_error": self.underlying_error,
            "stage": self.lifecycle_stage,
            "lifecycle_stage": self.lifecycle_stage,
            "backend_operation": self.backend_operation,
            "cause_type": self.cause_type,
            "cause_message": self.cause_message,
            "vhdx_path": str(self.vhdx_path) if self.vhdx_path is not None else None,
            "created_before_failure": self.created_before_failure,
            "attached_before_failure": self.attached_before_failure,
        }


class InvalidWindowsVhdxStorageRootError(WindowsVhdxNmlVolumeError):
    """Raised when the managed VHDX root is not an explicit safe directory."""


class WindowsVhdxPathOutsideStorageError(WindowsVhdxNmlVolumeError):
    """Raised when a VHDX path escapes the configured managed root."""


class WindowsVhdxIncompatibleError(WindowsVhdxNmlVolumeError):
    """Raised when an existing VHDX cannot safely serve this adapter."""


class WindowsVhdxAttachError(WindowsVhdxNmlVolumeError):
    """Raised when a managed VHDX cannot be attached or observed attached."""


class WindowsVhdxCreateError(WindowsVhdxNmlVolumeError):
    """Raised when a managed VHDX cannot be created safely."""


class WindowsVhdxInspectionError(WindowsVhdxNmlVolumeError):
    """Raised when managed VHDX state cannot be inspected safely."""


class WindowsVhdxMountError(WindowsVhdxNmlVolumeError):
    """Raised when a managed VHDX has no confirmed usable mount point."""


class WindowsVhdxInitializationError(WindowsVhdxNmlVolumeError):
    """Raised when a new or unformatted managed VHDX cannot be initialized."""


class WindowsVhdxFormatError(WindowsVhdxNmlVolumeError):
    """Raised when a new or unformatted managed VHDX cannot be formatted."""


class WindowsVhdxDetachError(WindowsVhdxNmlVolumeError):
    """Raised when a managed VHDX cannot be detached."""


class WindowsVhdxVolumeIdentityMismatchError(WindowsVhdxNmlVolumeError):
    """Raised when adapter state and the observed managed VHDX disagree."""


class WindowsVhdxMaterializationError(WindowsVhdxNmlVolumeError):
    """Raised when the mounted catalog cannot be atomically materialized."""


@dataclass(frozen=True, slots=True)
class WindowsVhdxNmlVolumeConfig:
    """Explicit configuration for one managed, non-physical VHDX."""

    storage_root: Path
    vhdx_path: Path
    logical_size_mb: int
    volume_label: str
    preferred_drive_letter: str | None = None
    timeout_seconds: float = 30.0
    filesystem: str = "NTFS"
    remove_vhdx_on_unpublish: bool = False

    def __post_init__(self) -> None:
        if not self.storage_root.is_absolute() or not self.vhdx_path.is_absolute():
            raise InvalidWindowsVhdxStorageRootError("storage_root and vhdx_path must be absolute")
        if not self.storage_root.name or self.logical_size_mb <= 0 or self.timeout_seconds <= 0:
            raise InvalidWindowsVhdxStorageRootError("storage root, size, and timeout must be valid")
        if self.vhdx_path.suffix.lower() != ".vhdx":
            raise WindowsVhdxPathOutsideStorageError("managed disk must be a .vhdx file")
        if not self.volume_label or not self.filesystem:
            raise InvalidWindowsVhdxStorageRootError("filesystem and volume_label must be non-empty")
        if self.preferred_drive_letter is not None and (
            len(self.preferred_drive_letter) != 1 or not self.preferred_drive_letter.isalpha()
        ):
            raise InvalidWindowsVhdxStorageRootError("preferred_drive_letter must be one letter")
        _require_within(self.vhdx_path, self.storage_root)


@dataclass(frozen=True, slots=True)
class WindowsVhdxVolumeInspection:
    """Observed state tied exclusively to one VHDX path."""

    vhdx_path: Path
    exists: bool
    attached: bool
    initialized: bool
    filesystem: str | None
    volume_label: str | None
    mount_point: Path | None
    logical_size_mb: int | None = None
    compatible: bool | None = None
    partition_present: bool | None = None
    access_path: Path | None = None
    physical_identity: VhdxFileIdentity | None = None


@runtime_checkable
class WindowsVirtualDiskBackend(Protocol):
    """Platform operations scoped by a VHDX path, never a physical disk id."""

    def create_vhdx(self, path: Path, logical_size_mb: int, timeout_seconds: float) -> None: ...
    def attach(self, path: Path, timeout_seconds: float) -> None: ...
    def initialize_if_needed(self, path: Path, timeout_seconds: float) -> None: ...
    def format_if_needed(self, path: Path, filesystem: str, label: str, timeout_seconds: float) -> None: ...
    def assign_mount(self, path: Path, preferred_drive_letter: str | None, timeout_seconds: float) -> Path: ...
    def inspect(self, path: Path) -> WindowsVhdxVolumeInspection: ...
    def detach(self, path: Path, timeout_seconds: float) -> None: ...
    def remove_vhdx(self, path: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class WindowsVhdxNmlVolumeState:
    source_id: str
    vhdx_path: Path
    mount_point: Path | None
    mounted: bool
    initialized: bool
    filesystem: str | None
    label: str | None
    snapshot_id: str
    invalidated: bool


@dataclass(frozen=True, slots=True)
class WindowsVhdxNmlVolumeResult:
    operation: ProjectionOperationResult
    state: WindowsVhdxNmlVolumeState
    materialization: NmlCatalogMaterializationResult | None = None


@dataclass(frozen=True, slots=True)
class WindowsVhdxNmlUnpublishResult:
    """Outcome of detaching one already-managed VHDX without deleting it."""

    vhdx_path: Path
    mount_before: Path | None
    mount_after: Path | None
    changed: bool
    warnings: tuple[str, ...] = ()


def _require_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WindowsVhdxPathOutsideStorageError(str(path)) from exc


class WindowsVhdxNmlVolumeAdapter:
    """Single-source lifecycle adapter for an injected managed VHDX backend."""

    def __init__(
        self,
        config: WindowsVhdxNmlVolumeConfig,
        backend: WindowsVirtualDiskBackend,
        *,
        lifecycle_sink: Callable[[str], None] | None = None,
        source_root: Path | None = None,
        ownership_ledger: VhdxOwnershipLedger | None = None,
    ) -> None:
        if not isinstance(backend, WindowsVirtualDiskBackend):
            raise TypeError("backend must implement WindowsVirtualDiskBackend")
        self._config = config
        self._backend = backend
        self._lifecycle_sink = lifecycle_sink
        self._source_root = source_root
        self._write_boundary: SourceWriteBoundary | None = None
        self._ownership_ledger = ownership_ledger
        self._state: WindowsVhdxNmlVolumeState | None = None

    def capabilities(self) -> ProjectionCapabilities:
        return ProjectionCapabilities(True, False, True, True, False, False)

    def publish(self, catalog: NmlCatalog) -> WindowsVhdxNmlVolumeResult:
        self._validate_catalog(catalog)
        if self._source_root is None:
            raise SourceWriteBoundaryError(WriteDenial.SOURCE_IDENTITY_UNAVAILABLE)
        boundary = SourceWriteBoundary(self._source_root)
        self._write_boundary = boundary
        if self._ownership_ledger is None:
            self._ownership_ledger = VhdxOwnershipLedger(
                self._config.storage_root, boundary=boundary
            )
        if hasattr(self._backend, "bind_write_boundary"):
            self._backend.bind_write_boundary(boundary)
        write_plan = WritePlan(WriteIntent.VHDX, (self._config.storage_root, self._config.vhdx_path))
        permit = boundary.authorize(write_plan)
        permit.close()
        if self._state is not None:
            raise SourceAlreadyPublishedError(self._state.source_id)
        created = False
        creation_record: VhdxOwnershipRecord | None = None
        attached = False
        lifecycle_stage = "inspect"
        backend_operation = "inspect"
        try:
            inspection = self._inspect()
            if not inspection.exists:
                creation_record = self._ownership_ledger.begin_create(
                    self._config.vhdx_path,
                    catalog.source_id,
                    inspection.physical_identity,
                )
                lifecycle_stage = "create"
                backend_operation = "create_vhdx"
                permit = boundary.authorize(write_plan)
                permit.revalidate_before_side_effect(self._config.vhdx_path)
                self._write_effect("create_vhdx", self._config.vhdx_path, self._config.logical_size_mb, self._config.timeout_seconds)
                permit.close()
                created = True
                self._record("created")
                lifecycle_stage = "inspect"
                backend_operation = "inspect"
                inspection = self._inspect(required_exists=True)
                creation_record = self._ownership_ledger.complete_create(
                    creation_record, inspection.physical_identity
                )
            else:
                try:
                    self._ownership_ledger.authorize(
                        self._config.vhdx_path,
                        inspection.physical_identity,
                        "reuse",
                    )
                except VhdxOwnershipError as exc:
                    raise WindowsVhdxIncompatibleError(
                        "preexisting VHDX ownership is not proved"
                    ) from exc
            attached = inspection.attached
            if not inspection.attached:
                lifecycle_stage = "attach"
                backend_operation = "attach"
                try:
                    self._write_effect("attach", self._config.vhdx_path, self._config.timeout_seconds)
                except Exception as attach_error:
                    try:
                        inspection = self._inspect(required_attached=True)
                    except Exception:
                        raise attach_error
                    attached = True
                    self._record("attached_reused")
                else:
                    attached = True
                    self._record("attached")
                    lifecycle_stage = "inspect"
                    backend_operation = "inspect"
                    inspection = self._inspect(required_attached=True)
            else:
                self._record("attached_reused")
            if not inspection.initialized and inspection.filesystem is not None:
                raise WindowsVhdxIncompatibleError("existing VHDX has a filesystem but is not initialized")
            if not inspection.initialized:
                lifecycle_stage = "initialize"
                backend_operation = "initialize_if_needed"
                self._write_effect("initialize_if_needed", self._config.vhdx_path, self._config.timeout_seconds)
                lifecycle_stage = "inspect"
                backend_operation = "inspect"
                inspection = self._wait_for(
                    lambda value: value.initialized and value.partition_present is True,
                    WindowsVhdxInitializationError,
                    "partition initialization",
                )
                self._record("initialized")
            elif inspection.partition_present is not True:
                raise WindowsVhdxIncompatibleError("existing initialized VHDX has no usable partition")
            self._record("partition_ready")
            if (
                not created
                and inspection.filesystem is None
                and inspection.mount_point is None
            ):
                self._record("mount_requested")
                lifecycle_stage = "mount"
                backend_operation = "assign_mount"
                self._write_effect("assign_mount",
                    self._config.vhdx_path,
                    self._config.preferred_drive_letter,
                    self._config.timeout_seconds,
                )
                lifecycle_stage = "inspect"
                backend_operation = "inspect"
                inspection = self._wait_for(
                    lambda value: value.mount_point is not None,
                    WindowsVhdxMountError,
                    "mount point",
                )
            if inspection.filesystem is None:
                if not created:
                    raise WindowsVhdxIncompatibleError("preexisting VHDX without a filesystem is not eligible for formatting")
                self._record("format_requested")
                lifecycle_stage = "format"
                backend_operation = "format_if_needed"
                self._write_effect("format_if_needed",
                    self._config.vhdx_path, self._config.filesystem, self._config.volume_label, self._config.timeout_seconds
                )
                lifecycle_stage = "inspect"
                backend_operation = "inspect"
                inspection = self._wait_for(
                    lambda value: value.filesystem == self._config.filesystem and value.volume_label == self._config.volume_label,
                    WindowsVhdxFormatError,
                    "filesystem and volume label",
                )
                self._record("format_completed")
            self._record("filesystem_ready")
            self._validate_compatibility(inspection, created)
            try:
                if inspection.mount_point is None:
                    self._record("mount_requested")
                    lifecycle_stage = "mount"
                    backend_operation = "assign_mount"
                    mount = self._write_effect("assign_mount",
                        self._config.vhdx_path, self._config.preferred_drive_letter, self._config.timeout_seconds
                    )
                else:
                    mount = inspection.mount_point
            except Exception as exc:
                raise WindowsVhdxMountError(
                    "unable to mount managed VHDX",
                    lifecycle_stage="mount",
                    backend_operation="assign_mount",
                    cause=exc,
                    vhdx_path=self._config.vhdx_path,
                    created_before_failure=created,
                    attached_before_failure=attached,
                ) from exc
            lifecycle_stage = "inspect"
            backend_operation = "inspect"
            inspection = self._wait_for(
                lambda value: value.mount_point is not None,
                WindowsVhdxMountError,
                "mount point",
            )
            mount = inspection.mount_point or mount
            self._validate_mount(mount)
            self._record("mount_ready")
            self._record("materialization_started")
            lifecycle_stage = "materialize"
            backend_operation = "materialize_nml_catalog"
            materialization = self._materialize(catalog, mount)
        except WindowsVhdxNmlVolumeError as exc:
            self._cleanup_after_failed_publish(creation_record)
            raise self._annotate_error(
                exc,
                lifecycle_stage=lifecycle_stage,
                backend_operation=backend_operation,
                created=created,
                attached=attached,
            )
        except Exception as exc:
            self._cleanup_after_failed_publish(creation_record)
            raise self._backend_failure(
                lifecycle_stage,
                backend_operation,
                exc,
                created=created,
                attached=attached,
            ) from exc
        self._state = WindowsVhdxNmlVolumeState(
            catalog.source_id, self._config.vhdx_path, mount, True, inspection.initialized,
            inspection.filesystem, inspection.volume_label, catalog.snapshot_id, False,
        )
        self._record_ownership_state(VhdxOwnershipState.EXPOSED)
        return WindowsVhdxNmlVolumeResult(ProjectionOperationResult.PUBLISHED, self._state, materialization)

    def refresh(self, catalog: NmlCatalog) -> WindowsVhdxNmlVolumeResult:
        self._validate_catalog(catalog)
        state = self._require_active(catalog.source_id)
        inspection = self._inspect(required_attached=True)
        if inspection.mount_point != state.mount_point:
            raise WindowsVhdxVolumeIdentityMismatchError("managed mount point changed")
        materialization = self._materialize(catalog, state.mount_point)
        operation = ProjectionOperationResult.REFRESHED if materialization.changed else ProjectionOperationResult.UNCHANGED
        self._state = replace(state, snapshot_id=catalog.snapshot_id)
        return WindowsVhdxNmlVolumeResult(operation, self._state, materialization)

    def invalidate(
        self, source_id: str, *, detach_authorization: object
    ) -> WindowsVhdxNmlVolumeResult:
        state = self._require_state(source_id)
        if state.invalidated:
            return WindowsVhdxNmlVolumeResult(ProjectionOperationResult.ALREADY_INVALIDATED, state)
        self._detach_if_attached(detach_authorization=detach_authorization)
        self._state = replace(state, mounted=False, invalidated=True)
        return WindowsVhdxNmlVolumeResult(ProjectionOperationResult.INVALIDATED, self._state)

    def unpublish(
        self, source_id: str, *, detach_authorization: object
    ) -> WindowsVhdxNmlVolumeResult:
        state = self._require_state(source_id)
        self._detach_if_attached(detach_authorization=detach_authorization)
        if self._config.remove_vhdx_on_unpublish:
            _require_within(self._config.vhdx_path, self._config.storage_root)
            self._write_effect(
                "remove_vhdx",
                self._config.vhdx_path,
                expected_generation=detach_authorization.generation,
            )
        self._state = None
        return WindowsVhdxNmlVolumeResult(ProjectionOperationResult.UNPUBLISHED, replace(state, mounted=False))

    def unpublish_existing(self, *, detach_authorization: object) -> WindowsVhdxNmlUnpublishResult:
        """Detach only this configured VHDX, preserving its catalog and file.

        Callers must provide the exact backend/path authorization returned by
        the managed lifecycle guard immediately before this operation.
        """
        self._require_detach_authorization(detach_authorization)
        inspection = self._inspect()
        if not inspection.exists:
            return WindowsVhdxNmlUnpublishResult(
                self._config.vhdx_path,
                None,
                None,
                False,
                ("managed_vhdx_not_found",),
            )
        if not inspection.attached:
            return WindowsVhdxNmlUnpublishResult(
                self._config.vhdx_path,
                inspection.mount_point,
                None,
                False,
            )

        mount_before = inspection.mount_point
        try:
            self._write_effect(
                "detach",
                self._config.vhdx_path,
                self._config.timeout_seconds,
                expected_generation=detach_authorization.generation,
            )
        except Exception as exc:
            try:
                after = self._inspect()
            except Exception:
                raise WindowsVhdxDetachError(
                    "unable to detach managed VHDX",
                    lifecycle_stage="detach",
                    backend_operation="detach",
                    cause=exc,
                    vhdx_path=self._config.vhdx_path,
                    created_before_failure=False,
                    attached_before_failure=True,
                ) from exc
            if after.exists and not after.attached:
                return WindowsVhdxNmlUnpublishResult(
                    self._config.vhdx_path, mount_before, None, False
                )
            raise WindowsVhdxDetachError(
                "unable to detach managed VHDX",
                lifecycle_stage="detach",
                backend_operation="detach",
                cause=exc,
                vhdx_path=self._config.vhdx_path,
                created_before_failure=False,
                attached_before_failure=True,
            ) from exc

        after = self._inspect()
        if not after.exists or after.attached:
            raise WindowsVhdxDetachError(
                "managed VHDX remained attached after detach",
                lifecycle_stage="detach",
                backend_operation="detach",
                vhdx_path=self._config.vhdx_path,
                created_before_failure=False,
                attached_before_failure=True,
            )
        return WindowsVhdxNmlUnpublishResult(
            self._config.vhdx_path, mount_before, None, True
        )

    def _validate_catalog(self, catalog: NmlCatalog) -> None:
        if not isinstance(catalog, NmlCatalog) or not catalog.source_id or not catalog.snapshot_id:
            raise TypeError("catalog must be an identified NmlCatalog")

    def _record(self, event: str) -> None:
        if self._lifecycle_sink is not None:
            self._lifecycle_sink(event)

    def _write_effect(
        self,
        method: str,
        *args: object,
        expected_generation: str | None = None,
        operation_id: str | None = None,
        allow_partial: bool = False,
    ) -> object:
        boundary = self._write_boundary
        if boundary is None:
            if self._source_root is not None:
                boundary = SourceWriteBoundary(self._source_root)
            elif self._ownership_ledger is not None:
                boundary = self._ownership_ledger.boundary_for(
                    self._config.vhdx_path
                )
            else:
                raise SourceWriteBoundaryError(WriteDenial.SOURCE_IDENTITY_UNAVAILABLE)
            self._write_boundary = boundary
            if hasattr(self._backend, "bind_write_boundary"):
                self._backend.bind_write_boundary(boundary)
        permit = boundary.authorize(WritePlan(WriteIntent.VHDX, (self._config.vhdx_path,)))
        capability: VhdxOwnershipCapability | None = None
        try:
            permit.revalidate_before_side_effect(self._config.vhdx_path)
            if method != "create_vhdx":
                if self._ownership_ledger is None:
                    self._ownership_ledger = VhdxOwnershipLedger(
                        self._config.storage_root, boundary=boundary
                    )
                inspection = self._inspect(required_exists=True)
                ownership_action = {
                    "initialize_if_needed": "initialize",
                    "format_if_needed": "format",
                }.get(method, method)
                capability = self._ownership_ledger.authorize(
                    self._config.vhdx_path,
                    inspection.physical_identity,
                    ownership_action,
                    expected_generation=expected_generation,
                    operation_id=operation_id,
                    allow_partial=allow_partial,
                )
                capability.consume(inspection.physical_identity)
            if hasattr(self._backend, "set_write_permit"):
                self._backend.set_write_permit(permit)
            if capability is not None and hasattr(
                self._backend, "set_ownership_capability"
            ):
                self._backend.set_ownership_capability(capability)
            result = getattr(self._backend, method)(*args)
            if capability is not None:
                state = {
                    "attach": VhdxOwnershipState.MANAGED_ATTACHED,
                    "detach": VhdxOwnershipState.MANAGED_DETACHED,
                    "remove_vhdx": VhdxOwnershipState.MISSING,
                }.get(method, VhdxOwnershipState.OWNED_VALID)
                self._ownership_ledger.record_effect(capability, state)
            return result
        finally:
            permit.close()

    def _record_ownership_state(self, state: VhdxOwnershipState) -> None:
        if self._ownership_ledger is None:
            raise WindowsVhdxVolumeIdentityMismatchError(
                "managed VHDX ownership ledger is unavailable"
            )
        inspection = self._inspect(required_exists=True)
        capability = self._ownership_ledger.authorize(
            self._config.vhdx_path, inspection.physical_identity, "state"
        )
        capability.consume(inspection.physical_identity)
        self._ownership_ledger.record_effect(capability, state)

    def _inspect(self, *, required_exists: bool = False, required_attached: bool = False) -> WindowsVhdxVolumeInspection:
        inspection = self._backend.inspect(self._config.vhdx_path)
        if inspection.vhdx_path != self._config.vhdx_path:
            raise WindowsVhdxVolumeIdentityMismatchError("backend inspection belongs to a different VHDX")
        if required_exists and not inspection.exists:
            raise WindowsVhdxIncompatibleError("managed VHDX was not created")
        if required_attached and (not inspection.exists or not inspection.attached):
            raise WindowsVhdxAttachError("managed VHDX is not attached")
        return inspection

    def _validate_compatibility(self, inspection: WindowsVhdxVolumeInspection, created: bool) -> None:
        if inspection.filesystem != self._config.filesystem or inspection.volume_label != self._config.volume_label:
            # A pre-existing filesystem is never reformatted: only a blank VHDX is eligible.
            if inspection.filesystem is not None or not created:
                raise WindowsVhdxIncompatibleError("existing VHDX filesystem or label is incompatible")
            raise WindowsVhdxFormatError("new managed VHDX was not formatted as expected")

    def _validate_mount(self, mount: Path | None) -> None:
        if mount is None or not mount.is_absolute() or not mount.exists() or not mount.is_dir() or mount.is_symlink():
            raise WindowsVhdxMountError("backend did not provide a usable mount point")

    def _wait_for(
        self,
        predicate: object,
        error_type: type[WindowsVhdxNmlVolumeError],
        expected_state: str,
    ) -> WindowsVhdxVolumeInspection:
        if not callable(predicate):
            raise TypeError("predicate must be callable")
        deadline = time.monotonic() + self._config.timeout_seconds
        last = self._inspect(required_attached=True)
        while not predicate(last):
            if time.monotonic() >= deadline:
                raise error_type(f"timed out waiting for {expected_state}; last inspection: {last!r}")
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            last = self._inspect(required_attached=True)
        return last

    def _materialize(self, catalog: NmlCatalog, mount: Path | None) -> NmlCatalogMaterializationResult:
        if mount is None:
            raise WindowsVhdxMountError("missing mount point")
        try:
            return materialize_nml_catalog(
                catalog, mount, expected_source_id=catalog.source_id,
                source_root=self._source_root,
            )
        except NmlCatalogMaterializationError as exc:
            raise WindowsVhdxMaterializationError("unable to materialize mounted NML catalog") from exc

    def _require_state(self, source_id: str) -> WindowsVhdxNmlVolumeState:
        if self._state is None or self._state.source_id != source_id:
            raise SourceNotPublishedError(source_id)
        return self._state

    def _require_active(self, source_id: str) -> WindowsVhdxNmlVolumeState:
        state = self._require_state(source_id)
        if state.invalidated:
            raise ProjectionInvalidatedError(source_id)
        return state

    def _require_detach_authorization(self, detach_authorization: object) -> None:
        if (
            getattr(detach_authorization, "backend", None) is not self._backend
            or getattr(detach_authorization, "path", None) != self._config.vhdx_path
            or not isinstance(getattr(detach_authorization, "generation", None), str)
        ):
            raise WindowsVhdxVolumeIdentityMismatchError(
                "managed VHDX detach requires the current lifecycle authorization"
            )

    def _detach_if_attached(self, *, detach_authorization: object) -> None:
        self._require_detach_authorization(detach_authorization)
        try:
            if self._inspect().attached:
                self._write_effect(
                    "detach",
                    self._config.vhdx_path,
                    self._config.timeout_seconds,
                    expected_generation=detach_authorization.generation,
                )
        except WindowsVhdxNmlVolumeError:
            raise
        except Exception as exc:
            raise WindowsVhdxDetachError("unable to detach managed VHDX") from exc

    def _detach_after_failed_publish(
        self, creation_record: VhdxOwnershipRecord
    ) -> None:
        try:
            if self._inspect().attached:
                self._write_effect(
                    "detach",
                    self._config.vhdx_path,
                    self._config.timeout_seconds,
                    expected_generation=creation_record.generation,
                    operation_id=creation_record.operation_id,
                    allow_partial=True,
                )
        except Exception:
            pass

    def _cleanup_after_failed_publish(
        self, creation_record: VhdxOwnershipRecord | None
    ) -> None:
        """Best-effort rollback that never replaces the original publication error."""
        if creation_record is None or self._ownership_ledger is None:
            return
        try:
            inspection = self._inspect()
            if inspection.exists and creation_record.state is VhdxOwnershipState.OWNERSHIP_PENDING:
                creation_record = self._ownership_ledger.mark_partial(
                    creation_record, inspection.physical_identity
                )
            elif not inspection.exists:
                self._ownership_ledger.mark_missing(creation_record)
                return
        except Exception:
            return
        self._detach_after_failed_publish(creation_record)
        try:
            inspection = self._backend.inspect(self._config.vhdx_path)
            if inspection.exists and not inspection.attached:
                _require_within(self._config.vhdx_path, self._config.storage_root)
                self._write_effect(
                    "remove_vhdx",
                    self._config.vhdx_path,
                    expected_generation=creation_record.generation,
                    operation_id=creation_record.operation_id,
                    allow_partial=True,
                )
        except Exception:
            return

    def _backend_failure(
        self,
        lifecycle_stage: str,
        backend_operation: str,
        cause: Exception,
        *,
        created: bool,
        attached: bool,
    ) -> WindowsVhdxNmlVolumeError:
        error_type: type[WindowsVhdxNmlVolumeError]
        if lifecycle_stage == "create":
            error_type = WindowsVhdxCreateError
        elif lifecycle_stage == "inspect":
            error_type = WindowsVhdxInspectionError
        elif lifecycle_stage == "attach":
            error_type = WindowsVhdxAttachError
        elif lifecycle_stage == "mount":
            error_type = WindowsVhdxMountError
        elif lifecycle_stage == "initialize":
            error_type = WindowsVhdxInitializationError
        elif lifecycle_stage == "format":
            error_type = WindowsVhdxFormatError
        else:
            error_type = WindowsVhdxMaterializationError
        return error_type(
            f"managed VHDX {backend_operation} failed",
            lifecycle_stage=lifecycle_stage,
            backend_operation=backend_operation,
            cause=cause,
            vhdx_path=self._config.vhdx_path,
            created_before_failure=created,
            attached_before_failure=attached,
        )

    def _annotate_error(
        self,
        error: WindowsVhdxNmlVolumeError,
        *,
        lifecycle_stage: str,
        backend_operation: str,
        created: bool,
        attached: bool,
    ) -> WindowsVhdxNmlVolumeError:
        if error.lifecycle_stage is None:
            error.lifecycle_stage = lifecycle_stage
        if error.backend_operation is None:
            error.backend_operation = backend_operation
        if error.vhdx_path is None:
            error.vhdx_path = self._config.vhdx_path
        if error.created_before_failure is None:
            error.created_before_failure = created
        if error.attached_before_failure is None:
            error.attached_before_failure = attached
        return error
