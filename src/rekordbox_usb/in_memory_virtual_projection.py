"""Synthetic, non-thread-safe lifecycle validation for virtual projections."""

from __future__ import annotations

from enum import StrEnum

from .virtual_projection import (
    InvalidProjectionSnapshotError,
    ProjectionCapabilities,
    ProjectionInvalidatedError,
    ProjectionSnapshot,
    SourceAlreadyPublishedError,
    SourceNotPublishedError,
    VirtualProjectionAdapter,
)
from .virtual_projection_builder import (
    ProjectionBuildInput,
    build_projection_snapshot,
)


class ProjectionCoordinatorError(ValueError):
    """Base error for coordinator lifecycle invariants."""


class ConflictingProjectionRevisionError(ProjectionCoordinatorError):
    """Raised when one revision identifies two different snapshots."""


class ProjectionOperationResult(StrEnum):
    """Stable outcomes of synthetic projection lifecycle operations."""

    PUBLISHED = "published"
    REFRESHED = "refreshed"
    UNCHANGED = "unchanged"
    INVALIDATED = "invalidated"
    ALREADY_INVALIDATED = "already_invalidated"
    UNPUBLISHED = "unpublished"


class InMemoryVirtualProjectionAdapter:
    """A deterministic, non-thread-safe adapter with no filesystem behaviour."""

    _CAPABILITIES = ProjectionCapabilities(
        read_only=True,
        random_access=False,
        dynamic_refresh=True,
        invalidation=True,
        stable_mount_name=False,
        multiple_sources=True,
    )

    def __init__(self) -> None:
        self._snapshots: dict[str, ProjectionSnapshot] = {}
        self._invalidated: set[str] = set()

    def publish(self, snapshot: ProjectionSnapshot) -> None:
        """Publish a previously unknown coherent source snapshot."""
        source_id = self._source_id(snapshot)
        if source_id in self._snapshots:
            raise SourceAlreadyPublishedError(source_id)
        self._snapshots[source_id] = snapshot

    def refresh(self, snapshot: ProjectionSnapshot) -> None:
        """Replace an active source snapshot while preserving its source identity."""
        source_id = self._source_id(snapshot)
        current = self._snapshots.get(source_id)
        if current is None:
            raise SourceNotPublishedError(source_id)
        if source_id in self._invalidated:
            raise ProjectionInvalidatedError(source_id)
        if current.source.volume_identity != snapshot.source.volume_identity:
            raise InvalidProjectionSnapshotError("refresh must preserve volume_identity")
        self._snapshots[source_id] = snapshot

    def invalidate(self, source_id: str) -> None:
        """Mark a source invalid; repeated invalidation is explicitly idempotent."""
        self._require_published(source_id)
        self._invalidated.add(source_id)

    def unpublish(self, source_id: str) -> None:
        """Remove an active or invalidated source projection."""
        self._require_published(source_id)
        del self._snapshots[source_id]
        self._invalidated.discard(source_id)

    def capabilities(self) -> ProjectionCapabilities:
        """Return the explicitly supported synthetic capabilities."""
        return self._CAPABILITIES

    def get_published_snapshot(self, source_id: str) -> ProjectionSnapshot:
        """Return an immutable snapshot for synthetic lifecycle assertions."""
        self._require_published(source_id)
        return self._snapshots[source_id]

    def published_source_ids(self) -> tuple[str, ...]:
        """Return source ids in deterministic order for synthetic assertions."""
        return tuple(sorted(self._snapshots))

    def is_invalidated(self, source_id: str) -> bool:
        """Report whether a published source is invalidated."""
        self._require_published(source_id)
        return source_id in self._invalidated

    @staticmethod
    def _source_id(snapshot: ProjectionSnapshot) -> str:
        if not isinstance(snapshot, ProjectionSnapshot):
            raise TypeError("snapshot must be a ProjectionSnapshot")
        return snapshot.source.source_id

    def _require_published(self, source_id: str) -> None:
        if source_id not in self._snapshots:
            raise SourceNotPublishedError(source_id)


class ProjectionCoordinator:
    """Build and drive one adapter without discovery, I/O, or implicit reactivation."""

    def __init__(self, adapter: VirtualProjectionAdapter) -> None:
        if not isinstance(adapter, VirtualProjectionAdapter):
            raise TypeError("adapter must implement VirtualProjectionAdapter")
        self._adapter = adapter
        self._snapshots: dict[str, ProjectionSnapshot] = {}
        self._invalidated: set[str] = set()

    def publish_or_refresh(
        self, build_input: ProjectionBuildInput
    ) -> ProjectionOperationResult:
        """Publish new input, refresh a new revision, or leave an equal snapshot unchanged."""
        snapshot = build_projection_snapshot(build_input)
        source_id = snapshot.source.source_id
        current = self._snapshots.get(source_id)
        if current is None:
            self._adapter.publish(snapshot)
            self._snapshots[source_id] = snapshot
            return ProjectionOperationResult.PUBLISHED
        if source_id in self._invalidated:
            raise ProjectionInvalidatedError(source_id)
        if current.snapshot_id == snapshot.snapshot_id:
            if current == snapshot:
                return ProjectionOperationResult.UNCHANGED
            raise ConflictingProjectionRevisionError(snapshot.snapshot_id)
        self._adapter.refresh(snapshot)
        self._snapshots[source_id] = snapshot
        return ProjectionOperationResult.REFRESHED

    def invalidate(self, source_id: str) -> ProjectionOperationResult:
        """Invalidate one coordinator-owned source idempotently."""
        self._require_managed(source_id)
        if source_id in self._invalidated:
            return ProjectionOperationResult.ALREADY_INVALIDATED
        self._adapter.invalidate(source_id)
        self._invalidated.add(source_id)
        return ProjectionOperationResult.INVALIDATED

    def unpublish(self, source_id: str) -> ProjectionOperationResult:
        """Unpublish one coordinator-owned active or invalidated source."""
        self._require_managed(source_id)
        self._adapter.unpublish(source_id)
        del self._snapshots[source_id]
        self._invalidated.discard(source_id)
        return ProjectionOperationResult.UNPUBLISHED

    def _require_managed(self, source_id: str) -> None:
        if source_id not in self._snapshots:
            raise SourceNotPublishedError(source_id)
