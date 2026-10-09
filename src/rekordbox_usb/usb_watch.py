"""Foreground reconciliation of Windows volume arrivals with USB publication."""

from __future__ import annotations

import ntpath
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .host_process import HostProcessProbe, HostProcessState
from .usb_identity import UsbIdentityStore
from .windows_drives import DriveInfo, PreflightStatus, RekordboxPreflightReport


# Windows commonly exposes a drive letter before the filesystem is ready.  These
# bounded defaults wait at most 3.75 seconds in addition to the initial probe.
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_DEBOUNCE_SECONDS = 1.0
DEFAULT_READINESS_ATTEMPTS = 5
DEFAULT_READINESS_INITIAL_DELAY_SECONDS = 0.25
DEFAULT_READINESS_BACKOFF_MULTIPLIER = 2.0
DEFAULT_READINESS_MAX_DELAY_SECONDS = 2.0
SHUTDOWN_CLEANUP_OPERATIONAL_EXIT_CODE = 3


def volume_key(root: str | Path) -> str:
    """Return one case-insensitive key for an explicit Windows mount root."""
    return ntpath.normcase(ntpath.normpath(str(root)))


class VolumeObserver(Protocol):
    """Read-only source of current mounted-volume snapshots."""

    def snapshot(self) -> tuple[DriveInfo, ...]: ...

    def close(self) -> None: ...


class VolumeReadinessProbe(Protocol):
    """Check whether an explicit mount exists and its root can be opened."""

    def is_accessible(self, root: Path) -> bool: ...


class Publisher(Protocol):
    def __call__(self, *, volume_root: str | Path): ...


class Unpublisher(Protocol):
    def __call__(self, *, publication: ActiveUsbPublication): ...


class PreflightRunner(Protocol):
    def __call__(self, *, volume_root: str | Path) -> RekordboxPreflightReport: ...


@dataclass(frozen=True, slots=True)
class WatchUsbConfig:
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS
    readiness_attempts: int = DEFAULT_READINESS_ATTEMPTS
    readiness_initial_delay_seconds: float = DEFAULT_READINESS_INITIAL_DELAY_SECONDS
    readiness_backoff_multiplier: float = DEFAULT_READINESS_BACKOFF_MULTIPLIER
    readiness_max_delay_seconds: float = DEFAULT_READINESS_MAX_DELAY_SECONDS
    publish_existing: bool = False
    retry_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        if self.debounce_seconds < 0:
            raise ValueError("debounce_seconds must not be negative")
        if self.readiness_attempts < 1:
            raise ValueError("readiness_attempts must be at least one")
        if self.readiness_initial_delay_seconds < 0:
            raise ValueError("readiness_initial_delay_seconds must not be negative")
        if self.readiness_backoff_multiplier < 1:
            raise ValueError("readiness_backoff_multiplier must be at least one")
        if self.readiness_max_delay_seconds < 0:
            raise ValueError("readiness_max_delay_seconds must not be negative")
        if self.retry_seconds is not None and self.retry_seconds <= 0:
            raise ValueError("retry_seconds must be positive")


@dataclass(frozen=True, slots=True)
class WatchUsbEvent:
    event: str
    volume: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"event": self.event}
        if self.volume is not None:
            payload["volume"] = self.volume
        payload.update(self.details)
        return payload


@dataclass(frozen=True, slots=True)
class ActiveUsbPublication:
    """Exact, already-authorized association retained only for this process."""

    source_volume: str
    volume: str
    identity_id: str
    source_id: str
    vhdx_path: Path
    vhdx_mount: Path | None

    @classmethod
    def from_result(cls, volume: DriveInfo, result: object) -> ActiveUsbPublication:
        identity_id = str(getattr(result, "identity_id", ""))
        source_id = str(getattr(result, "source_id", ""))
        vhdx_path = Path(getattr(result, "vhdx_path", ""))
        vhdx_mount_value = getattr(result, "vhdx_mount", None)
        if not identity_id or not source_id or not vhdx_path.is_absolute():
            raise ValueError("successful publication did not return a complete managed reference")
        return cls(
            source_volume=volume_key(volume.root),
            volume=str(volume.root),
            identity_id=identity_id,
            source_id=source_id,
            vhdx_path=vhdx_path,
            vhdx_mount=Path(vhdx_mount_value) if vhdx_mount_value is not None else None,
        )


class ActivePublicationRegistry:
    """In-memory registry keyed by the canonical physical source mount."""

    def __init__(self) -> None:
        self._by_source_volume: dict[str, ActiveUsbPublication] = {}

    def register(self, publication: ActiveUsbPublication) -> None:
        for existing in self.snapshot():
            if existing.source_volume != publication.source_volume and (
                existing.identity_id == publication.identity_id
                or existing.source_id == publication.source_id
                or existing.vhdx_path == publication.vhdx_path
            ):
                raise ValueError("publication identity or managed path is already registered")
        self._by_source_volume[publication.source_volume] = publication

    def get(self, source_volume: str | Path) -> ActiveUsbPublication | None:
        return self._by_source_volume.get(volume_key(source_volume))

    def remove(self, source_volume: str | Path) -> ActiveUsbPublication | None:
        return self._by_source_volume.pop(volume_key(source_volume), None)

    def snapshot(self) -> tuple[ActiveUsbPublication, ...]:
        return tuple(self._by_source_volume[key] for key in sorted(self._by_source_volume))


@dataclass(frozen=True, slots=True)
class StartupRecoveryUnpublication:
    """One exact managed VHDX detached during startup reconciliation."""

    identity_id: str
    source_id: str
    vhdx_path: Path
    vhdx_mount_before: Path | None
    vhdx_mount_after: Path | None
    changed: bool


@dataclass(frozen=True, slots=True)
class StartupRecoveryFailure:
    """Structured, non-fatal diagnostic for one recovery candidate."""

    vhdx_path: Path | None
    error: str
    message: str
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StartupRecoveryDeferred:
    """One owned orphan preserved because the protected host may be using it."""

    identity_id: str
    source_id: str
    vhdx_path: Path
    vhdx_mount: Path | None
    reason: str


@dataclass(frozen=True, slots=True)
class StartupRecoveryResult:
    """Independent startup outcomes returned by the Storage composition."""

    reconciled: tuple[ActiveUsbPublication, ...] = ()
    unpublished: tuple[StartupRecoveryUnpublication, ...] = ()
    errors: tuple[StartupRecoveryFailure, ...] = ()
    deferred: tuple[StartupRecoveryDeferred, ...] = ()


class StartupRecoverer(Protocol):
    def __call__(
        self,
        *,
        volumes: tuple[DriveInfo, ...],
        host_state: HostProcessState,
    ) -> StartupRecoveryResult: ...


class PollingWindowsVolumeObserver:
    """Dependency-free observer backed by the existing Win32 volume inventory."""

    def __init__(self, enumerate_volumes: Callable[[], list[DriveInfo]]) -> None:
        self._enumerate_volumes = enumerate_volumes
        self.closed = False

    def snapshot(self) -> tuple[DriveInfo, ...]:
        if self.closed:
            return ()
        return tuple(self._enumerate_volumes())

    def close(self) -> None:
        self.closed = True


class DirectoryReadinessProbe:
    """Open only the explicit volume root; never recurse or inspect its contents."""

    def is_accessible(self, root: Path) -> bool:
        if not os.path.isdir(root):
            return False
        try:
            with os.scandir(root):
                return True
        except OSError:
            return False


@dataclass(slots=True)
class _PendingArrival:
    volume: DriveInfo
    detected_at: float


@dataclass(slots=True)
class DeferredUsbUnpublication:
    """One active publication retained until host-safe detach is possible."""

    publication: ActiveUsbPublication
    reason: str
    restored_volume: DriveInfo | None = None


class WatchUsbRuntime:
    """Reconcile snapshots and publish each eligible arrival independently."""

    def __init__(
        self,
        *,
        observer: VolumeObserver,
        preflight_runner: PreflightRunner,
        publisher: Publisher,
        event_sink: Callable[[WatchUsbEvent], None],
        unpublisher: Unpublisher | None = None,
        config: WatchUsbConfig | None = None,
        publication_registry: ActivePublicationRegistry | None = None,
        readiness_probe: VolumeReadinessProbe | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        error_details: Callable[[Exception], Mapping[str, object]] | None = None,
        startup_recoverer: StartupRecoverer | None = None,
        shutdown_unpublisher: Unpublisher | None = None,
        host_process_probe: HostProcessProbe | None = None,
        eligible_volume: Callable[[DriveInfo], bool] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        on_startup_complete: Callable[[], None] | None = None,
    ) -> None:
        self._observer = observer
        self._preflight_runner = preflight_runner
        self._publisher = publisher
        self._unpublisher = unpublisher
        self._event_sink = event_sink
        self._config = config or WatchUsbConfig()
        self._publication_registry = publication_registry or ActivePublicationRegistry()
        self._readiness_probe = readiness_probe or DirectoryReadinessProbe()
        self._clock = clock
        self._sleep = sleep
        self._error_details = error_details or self._default_error_details
        self._startup_recoverer = startup_recoverer
        self._shutdown_unpublisher = shutdown_unpublisher or unpublisher
        self._host_process_probe = host_process_probe
        self._deferred_by_identity: dict[str, DeferredUsbUnpublication] = {}
        self._startup_recovery_deferred = False
        self._eligible_volume = eligible_volume
        self._retry_requested: set[str] = set()
        self._stop_requested = stop_requested
        self._on_startup_complete = on_startup_complete

    @property
    def active_publications(self) -> tuple[ActiveUsbPublication, ...]:
        return self._publication_registry.snapshot()

    @property
    def deferred_unpublications(self) -> tuple[DeferredUsbUnpublication, ...]:
        return tuple(
            self._deferred_by_identity[key]
            for key in sorted(self._deferred_by_identity)
        )

    @staticmethod
    def _default_error_details(exc: Exception) -> Mapping[str, object]:
        return {"error": type(exc).__name__, "message": str(exc) or type(exc).__name__}

    def _emit(
        self,
        event: str,
        volume: DriveInfo | str | Path | None = None,
        **details: object,
    ) -> None:
        self._event_sink(
            WatchUsbEvent(
                event,
                (
                    None
                    if volume is None
                    else str(volume.root)
                    if isinstance(volume, DriveInfo)
                    else str(volume)
                ),
                details,
            )
        )

    @staticmethod
    def _snapshot_map(items: tuple[DriveInfo, ...]) -> dict[str, DriveInfo]:
        return {
            volume_key(item.root): item
            for item in items
            if str(item.root).strip() and ntpath.splitdrive(str(item.root))[0]
        }

    def _wait_until_ready(self, volume: DriveInfo) -> bool:
        delay = self._config.readiness_initial_delay_seconds
        for attempt in range(1, self._config.readiness_attempts + 1):
            if self._readiness_probe.is_accessible(volume.root):
                return True
            if attempt < self._config.readiness_attempts:
                self._sleep(delay)
                delay = min(
                    delay * self._config.readiness_backoff_multiplier,
                    self._config.readiness_max_delay_seconds,
                )
        return False

    def _observe_host(self, context: str) -> HostProcessState:
        if self._host_process_probe is None:
            return HostProcessState.NOT_RUNNING
        try:
            state = self._host_process_probe.state()
            if not isinstance(state, HostProcessState):
                raise TypeError("host process probe returned an invalid state")
            if state is HostProcessState.UNKNOWN:
                reporter = getattr(self._host_process_probe, "diagnostic", None)
                diagnostic = reporter() if callable(reporter) else {}
                if not isinstance(diagnostic, Mapping):
                    diagnostic = {}
                details = {str(key): value for key, value in diagnostic.items()}
                details.pop("context", None)
                details.pop("state", None)
                self._emit(
                    "HOST_PROCESS_STATE_UNKNOWN",
                    context=context,
                    state=HostProcessState.UNKNOWN.value,
                    **details,
                )
                return HostProcessState.UNKNOWN
            return state
        except Exception as exc:
            diagnostic = dict(self._error_details(exc))
            diagnostic.pop("context", None)
            diagnostic.pop("state", None)
            self._emit(
                "HOST_PROCESS_STATE_UNKNOWN",
                context=context,
                state=HostProcessState.UNKNOWN.value,
                **diagnostic,
            )
            return HostProcessState.UNKNOWN

    @staticmethod
    def _host_defer_reason(state: HostProcessState) -> str:
        return (
            "TRAKTOR_RUNNING"
            if state is HostProcessState.RUNNING
            else "HOST_STATE_UNKNOWN"
        )

    @staticmethod
    def _publication_reference(
        publication: ActiveUsbPublication,
    ) -> dict[str, object]:
        return {
            "identity_id": publication.identity_id,
            "source_id": publication.source_id,
            "vhdx_mount": (
                str(publication.vhdx_mount)
                if publication.vhdx_mount is not None
                else None
            ),
            "vhdx_path": str(publication.vhdx_path),
        }

    def _handle_deferred_source_arrival(
        self,
        volume: DriveInfo,
        *,
        serial: str | None,
    ) -> bool:
        if not self._deferred_by_identity:
            return False
        registered_at_root = self._publication_registry.get(volume.root)
        try:
            identity_id = UsbIdentityStore._identity_id(serial)
        except ValueError as exc:
            self._emit(
                "USB_SOURCE_RESTORE_ERROR",
                volume,
                reason="STABLE_IDENTITY_UNAVAILABLE",
                diagnostic=dict(self._error_details(exc)),
            )
            return True

        deferred = self._deferred_by_identity.get(identity_id)
        if deferred is None:
            if (
                registered_at_root is not None
                and registered_at_root.identity_id in self._deferred_by_identity
            ):
                self._emit(
                    "USB_SOURCE_RESTORE_ERROR",
                    volume,
                    reason="STABLE_IDENTITY_MISMATCH",
                    expected_identity_id=registered_at_root.identity_id,
                    observed_identity_id=identity_id,
                    **self._publication_reference(registered_at_root),
                )
                self._retry_requested.add(volume_key(volume.root))
                return True
            return False

        publication = deferred.publication
        reference = self._publication_reference(publication)
        if volume_key(volume.root) == publication.source_volume:
            del self._deferred_by_identity[identity_id]
            self._emit(
                "USB_SOURCE_RESTORED",
                volume,
                **reference,
            )
            return True

        deferred.restored_volume = volume
        self._emit(
            "USB_SOURCE_RELOCATION_DEFERRED",
            volume,
            reason=deferred.reason,
            previous_volume=publication.volume,
            **reference,
        )
        return True

    def _handle_arrival(self, volume: DriveInfo) -> None:
        if self._is_managed_vhdx(volume):
            self._emit("VOLUME_IGNORED", volume, reason="MANAGED_VHDX")
            return
        if self._eligible_volume is not None and not self._eligible_volume(volume):
            self._emit("VOLUME_IGNORED", volume, reason="INELIGIBLE_SOURCE")
            return
        if not self._wait_until_ready(volume):
            self._emit(
                "VOLUME_ERROR",
                volume,
                reason="VOLUME_NOT_READY",
                attempts=self._config.readiness_attempts,
            )
            self._retry_requested.add(volume_key(volume.root))
            return

        try:
            report = self._preflight_runner(volume_root=volume.root)
        except Exception as exc:
            self._emit(
                "VOLUME_ERROR",
                volume,
                reason="PREFLIGHT_FAILED",
                diagnostic=dict(self._error_details(exc)),
            )
            self._retry_requested.add(volume_key(volume.root))
            return
        if len(report.volumes) != 1:
            self._emit("VOLUME_IGNORED", volume, reason="VOLUME_DISAPPEARED")
            return

        item = report.volumes[0]
        if volume.serial is not None and item.serial != volume.serial:
            self._emit("VOLUME_ERROR", volume, reason="SOURCE_IDENTITY_CHANGED")
            self._retry_requested.add(volume_key(volume.root))
            return
        if item.status == PreflightStatus.UNSUPPORTED.value:
            self._emit("VOLUME_IGNORED", volume, reason="NOT_REKORDBOX_USB")
            self._retry_requested.add(volume_key(volume.root))
            return
        if item.status == PreflightStatus.BLOCKED.value:
            self._emit(
                "VOLUME_IGNORED",
                volume,
                reason="PREFLIGHT_BLOCKED",
                reasons=list(item.reasons),
                warnings=list(item.warnings),
            )
            self._retry_requested.add(volume_key(volume.root))
            return
        if volume.drive_type == 3 and volume.bus_type != 7:
            self._emit("VOLUME_IGNORED", volume, reason="INTERNAL_DISK")
            return
        if item.status not in {
            PreflightStatus.READY.value,
            PreflightStatus.READY_WITH_WARNINGS.value,
        }:
            self._emit("VOLUME_ERROR", volume, reason="UNKNOWN_PREFLIGHT_STATUS")
            return
        if self._handle_deferred_source_arrival(
            volume,
            serial=item.serial,
        ):
            return
        identity_id = UsbIdentityStore._identity_id(item.serial) if item.serial else None
        if identity_id is not None and any(
            active.identity_id == identity_id and active.source_volume != volume_key(volume.root)
            for active in self.active_publications
        ):
            self._emit("VOLUME_ERROR", volume, reason="AMBIGUOUS_SOURCE_IDENTITY")
            self._retry_requested.add(volume_key(volume.root))
            return

        self._emit(
            "USB_PUBLICATION_STARTED",
            volume,
            preflight_status=item.status.upper(),
            warnings=list(item.warnings),
        )
        try:
            result = self._publisher(volume_root=volume.root)
        except Exception as exc:
            self._emit(
                "VOLUME_ERROR",
                volume,
                reason="PUBLISH_FAILED",
                diagnostic=dict(self._error_details(exc)),
            )
            self._retry_requested.add(volume_key(volume.root))
            return
        try:
            publication = ActiveUsbPublication.from_result(volume, result)
        except (TypeError, ValueError) as exc:
            self._emit(
                "VOLUME_ERROR",
                volume,
                reason="INCOMPLETE_PUBLICATION_REFERENCE",
                diagnostic=dict(self._error_details(exc)),
            )
            return
        self._emit(
            "USB_PUBLISHED",
            volume,
            status=result.status,
            changed=result.changed,
            identity_id=result.identity_id,
            source_id=result.source_id,
            vhdx_path=str(result.vhdx_path),
            vhdx_mount=str(result.vhdx_mount) if result.vhdx_mount else None,
            warnings=list(result.warnings),
        )
        self._publication_registry.register(publication)

    @staticmethod
    def _is_managed_vhdx(volume: DriveInfo) -> bool:
        return volume.bus_type in (14, 15) or (volume.label or "").upper().startswith(
            ("TRAKTORBOX", "TRAKFROM_BX")
        )

    def _unpublish_publication(
        self,
        publication: ActiveUsbPublication,
    ) -> bool:
        reference = self._publication_reference(publication)
        self._emit(
            "USB_UNPUBLICATION_STARTED",
            publication.volume,
            **reference,
        )
        try:
            result = self._unpublisher(publication=publication)
        except Exception as exc:
            error = dict(self._error_details(exc))
            error.update(reference)
            self._emit(
                "USB_UNPUBLICATION_ERROR",
                publication.volume,
                **error,
            )
            return False
        self._emit(
            "USB_UNPUBLISHED",
            publication.volume,
            **reference,
            changed=result.changed,
            vhdx_mount_before=(
                str(result.vhdx_mount_before)
                if result.vhdx_mount_before is not None
                else None
            ),
            vhdx_mount_after=(
                str(result.vhdx_mount_after)
                if result.vhdx_mount_after is not None
                else None
            ),
        )
        self._publication_registry.remove(publication.source_volume)
        self._deferred_by_identity.pop(publication.identity_id, None)
        return True

    def _handle_removal(self, volume: DriveInfo) -> None:
        if self._is_managed_vhdx(volume):
            return
        self._emit("VOLUME_REMOVED", volume)
        removed_key = volume_key(volume.root)
        for deferred in self._deferred_by_identity.values():
            if (
                deferred.restored_volume is not None
                and volume_key(deferred.restored_volume.root) == removed_key
            ):
                deferred.restored_volume = None
                return

        publication = self._publication_registry.get(volume.root)
        if publication is None or self._unpublisher is None:
            return
        if publication.identity_id in self._deferred_by_identity:
            return

        host_state = self._observe_host("USB_REMOVAL")
        if host_state is not HostProcessState.NOT_RUNNING:
            reason = self._host_defer_reason(host_state)
            self._deferred_by_identity[publication.identity_id] = (
                DeferredUsbUnpublication(publication, reason)
            )
            self._emit(
                "USB_UNPUBLICATION_DEFERRED",
                volume,
                reason=reason,
                **self._publication_reference(publication),
            )
            return
        if not self._unpublish_publication(publication) and self._config.retry_seconds is not None:
            self._deferred_by_identity[publication.identity_id] = DeferredUsbUnpublication(
                publication, "DETACH_RETRY_REQUIRED"
            )

    def _run_startup_recovery(
        self,
        volumes: tuple[DriveInfo, ...],
        host_state: HostProcessState,
    ) -> None:
        if self._startup_recoverer is None:
            return
        self._emit("STARTUP_RECOVERY_STARTED")
        try:
            result = self._startup_recoverer(
                volumes=volumes,
                host_state=host_state,
            )
        except Exception as exc:
            self._startup_recovery_deferred = False
            diagnostic = dict(self._error_details(exc))
            self._emit(
                "STARTUP_RECOVERY_ERROR",
                vhdx_path=diagnostic.pop("vhdx_path", None),
                **diagnostic,
            )
            self._emit(
                "STARTUP_RECOVERY_COMPLETED",
                reconciled=0,
                unpublished=0,
                errors=1,
                deferred=0,
            )
            return

        self._startup_recovery_deferred = bool(result.deferred)
        for publication in result.reconciled:
            self._publication_registry.register(publication)
            self._emit(
                "MANAGED_VHDX_RECONCILED",
                identity_id=publication.identity_id,
                source_id=publication.source_id,
                volume=publication.volume,
                vhdx_mount=(
                    str(publication.vhdx_mount)
                    if publication.vhdx_mount is not None
                    else None
                ),
                vhdx_path=str(publication.vhdx_path),
            )
        for item in result.unpublished:
            self._emit(
                "ORPHANED_VHDX_UNPUBLISHED",
                changed=item.changed,
                identity_id=item.identity_id,
                source_id=item.source_id,
                vhdx_mount_before=(
                    str(item.vhdx_mount_before)
                    if item.vhdx_mount_before is not None
                    else None
                ),
                vhdx_mount_after=(
                    str(item.vhdx_mount_after)
                    if item.vhdx_mount_after is not None
                    else None
                ),
                vhdx_path=str(item.vhdx_path),
            )
        for item in result.deferred:
            self._emit(
                "STARTUP_RECOVERY_DEFERRED",
                reason=item.reason,
                identity_id=item.identity_id,
                source_id=item.source_id,
                vhdx_mount=(
                    str(item.vhdx_mount)
                    if item.vhdx_mount is not None
                    else None
                ),
                vhdx_path=str(item.vhdx_path),
            )
        for item in result.errors:
            details = dict(item.details)
            details.pop("vhdx_path", None)
            details.pop("error", None)
            details.pop("message", None)
            self._emit(
                "STARTUP_RECOVERY_ERROR",
                vhdx_path=str(item.vhdx_path) if item.vhdx_path is not None else None,
                error=item.error,
                message=item.message,
                **details,
            )
        self._emit(
            "STARTUP_RECOVERY_COMPLETED",
            reconciled=len(result.reconciled),
            unpublished=len(result.unpublished),
            errors=len(result.errors),
            deferred=len(result.deferred),
        )

    def _process_deferred_unpublications(
        self,
        current: Mapping[str, DriveInfo],
    ) -> None:
        if not self._deferred_by_identity and not self._startup_recovery_deferred:
            return
        host_state = self._observe_host("DEFERRED_UNPUBLICATION")
        if host_state is not HostProcessState.NOT_RUNNING:
            return

        for identity_id in tuple(sorted(self._deferred_by_identity)):
            deferred = self._deferred_by_identity.get(identity_id)
            if deferred is None:
                continue
            restored = deferred.restored_volume
            if (
                restored is not None
                and volume_key(restored.root) not in current
            ):
                deferred.restored_volume = None
                restored = None
            if not self._unpublish_publication(deferred.publication):
                continue
            if restored is not None:
                self._handle_arrival(restored)

        if self._startup_recovery_deferred and not self._deferred_by_identity:
            self._run_startup_recovery(
                tuple(current.values()),
                HostProcessState.NOT_RUNNING,
            )

    @staticmethod
    def _validate_shutdown_publication(publication: ActiveUsbPublication) -> None:
        if (
            not publication.identity_id
            or publication.source_id
            != f"rekordbox-usb:{publication.identity_id}"
            or not publication.vhdx_path.is_absolute()
            or publication.source_volume != volume_key(publication.volume)
        ):
            raise ValueError(
                "active publication is not a complete authorized managed reference"
            )

    @staticmethod
    def _validate_shutdown_result(
        publication: ActiveUsbPublication,
        result: object,
    ) -> bool:
        if (
            getattr(result, "identity_id", None) != publication.identity_id
            or getattr(result, "source_id", None) != publication.source_id
            or Path(getattr(result, "vhdx_path", "")) != publication.vhdx_path
            or not isinstance(getattr(result, "changed", None), bool)
            or not hasattr(result, "vhdx_mount_before")
            or not hasattr(result, "vhdx_mount_after")
        ):
            raise ValueError(
                "shutdown detach result does not match the active publication"
            )
        return result.changed

    def _run_shutdown_cleanup(self, observer_error: Exception | None = None) -> int:
        publications = self._publication_registry.snapshot()
        self._emit(
            "SHUTDOWN_CLEANUP_STARTED",
            active_publications=len(publications),
        )
        cleaned = 0
        already_detached = 0
        errors = 0
        deferred = 0
        if observer_error is not None:
            diagnostic = dict(self._error_details(observer_error))
            diagnostic.pop("identity_id", None)
            diagnostic.pop("source_id", None)
            diagnostic.pop("vhdx_path", None)
            self._emit(
                "SHUTDOWN_CLEANUP_ERROR",
                identity_id=None,
                source_id=None,
                vhdx_path=None,
                **diagnostic,
            )
            errors += 1

        host_state = (
            self._observe_host("SHUTDOWN_CLEANUP")
            if publications
            else HostProcessState.NOT_RUNNING
        )
        if host_state is HostProcessState.UNKNOWN:
            errors += 1

        for publication in publications:
            reference = self._publication_reference(publication)
            if host_state is not HostProcessState.NOT_RUNNING:
                self._emit(
                    "SHUTDOWN_CLEANUP_DEFERRED",
                    publication.volume,
                    reason=self._host_defer_reason(host_state),
                    **reference,
                )
                deferred += 1
                continue
            self._emit(
                "SHUTDOWN_UNPUBLICATION_STARTED",
                publication.volume,
                **reference,
            )
            try:
                self._validate_shutdown_publication(publication)
                if self._shutdown_unpublisher is None:
                    raise RuntimeError(
                        "shutdown cleanup has no managed VHDX unpublisher"
                    )
                result = self._shutdown_unpublisher(publication=publication)
                changed = self._validate_shutdown_result(publication, result)
            except Exception as exc:
                diagnostic = dict(self._error_details(exc))
                diagnostic.update(reference)
                self._emit("SHUTDOWN_CLEANUP_ERROR", **diagnostic)
                errors += 1
                continue

            self._emit(
                "SHUTDOWN_UNPUBLISHED",
                **reference,
                changed=changed,
                vhdx_mount_before=(
                    str(result.vhdx_mount_before)
                    if result.vhdx_mount_before is not None
                    else None
                ),
                vhdx_mount_after=(
                    str(result.vhdx_mount_after)
                    if result.vhdx_mount_after is not None
                    else None
                ),
            )
            self._publication_registry.remove(publication.source_volume)
            if changed:
                cleaned += 1
            else:
                already_detached += 1

        self._emit(
            "SHUTDOWN_CLEANUP_COMPLETED",
            cleaned=cleaned,
            already_detached=already_detached,
            errors=errors,
            deferred=deferred,
        )
        return SHUTDOWN_CLEANUP_OPERATIONAL_EXIT_CODE if errors else 0

    def run(self, *, max_cycles: int | None = None) -> int:
        """Run until Ctrl+C; ``max_cycles`` exists only for deterministic tests."""
        pending: dict[str, _PendingArrival] = {}
        shutdown_requested = False
        result_code = 0
        try:
            try:
                known = self._snapshot_map(self._observer.snapshot())
            except Exception as exc:
                if self._config.retry_seconds is None:
                    raise
                known = {}
                self._emit("WATCH_ERROR", diagnostic=dict(self._error_details(exc)))
            startup_host_state = (
                self._observe_host("STARTUP_RECOVERY")
                if self._startup_recoverer is not None
                else HostProcessState.NOT_RUNNING
            )
            self._run_startup_recovery(
                tuple(known.values()),
                startup_host_state,
            )
            self._emit("WATCH_STARTED")
            for volume in known.values():
                self._emit("VOLUME_KNOWN", volume)
                if (
                    self._config.publish_existing
                    and self._publication_registry.get(volume.root) is None
                ):
                    self._emit("VOLUME_ARRIVED", volume, existing=True)
                    pending[volume_key(volume.root)] = _PendingArrival(volume, self._clock())

            cycles = 0
            initial_pending = set(pending)
            startup_reported = False
            while max_cycles is None or cycles < max_cycles:
                if self._stop_requested is not None and self._stop_requested():
                    shutdown_requested = True
                    break
                try:
                    current = self._snapshot_map(self._observer.snapshot())
                except Exception as exc:
                    if self._config.retry_seconds is None:
                        raise
                    self._emit("WATCH_ERROR", diagnostic=dict(self._error_details(exc)))
                    cycles += 1
                    self._sleep(self._config.poll_interval_seconds)
                    continue
                now = self._clock()

                # A letter may be reused without an intervening empty snapshot.
                for key in known.keys() & current.keys():
                    if known[key].serial != current[key].serial:
                        self._handle_removal(known[key])
                        pending.pop(key, None)
                        self._emit("VOLUME_ARRIVED", current[key], existing=False)
                        pending[key] = _PendingArrival(current[key], now)

                for key, volume in current.items():
                    if key not in known:
                        self._emit("VOLUME_ARRIVED", volume, existing=False)
                        pending.setdefault(key, _PendingArrival(volume, now))

                for key in sorted(known.keys() - current.keys()):
                    self._handle_removal(known[key])

                for key in tuple(pending):
                    if key not in current:
                        self._emit(
                            "VOLUME_IGNORED",
                            pending[key].volume,
                            reason="VOLUME_DISAPPEARED",
                        )
                        del pending[key]
                        initial_pending.discard(key)

                known = current
                ready = [
                    key
                    for key, arrival in pending.items()
                    if now - arrival.detected_at >= self._config.debounce_seconds
                ]
                for key in ready:
                    arrival = pending.pop(key)
                    self._retry_requested.discard(key)
                    self._handle_arrival(current[key])
                    initial_pending.discard(key)
                    if key in self._retry_requested and self._config.retry_seconds is not None:
                        pending[key] = _PendingArrival(
                            current[key], now + self._config.retry_seconds - self._config.debounce_seconds
                        )

                self._process_deferred_unpublications(current)
                if not startup_reported and not initial_pending:
                    startup_reported = True
                    if self._on_startup_complete is not None:
                        self._on_startup_complete()
                cycles += 1
                if max_cycles is None or cycles < max_cycles:
                    self._sleep(self._config.poll_interval_seconds)
        except KeyboardInterrupt:
            shutdown_requested = True
        except BaseException:
            # A supervised launcher must clean up its publications on startup failure too.
            shutdown_requested = self._stop_requested is not None
            raise
        finally:
            if shutdown_requested:
                observer_error: Exception | None = None
                try:
                    self._observer.close()
                except Exception as exc:
                    observer_error = exc
                result_code = self._run_shutdown_cleanup(observer_error)
            else:
                self._observer.close()
            self._emit("WATCH_STOPPED")
        return result_code
