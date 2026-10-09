"""Legacy-only public USB to managed-NML composition."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from functools import wraps
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable

from .host_process import HostProcessProbe, HostProcessState, WindowsTraktorProcessProbe
from .legacy_compatibility import CompatibilityFailure
from .legacy_service_provider import LegacyServiceProvider
from .library_models import UnifiedLibrary
from .library_projection import build_projection_input_from_library
from .library_provider_registry import LibraryProviderRegistry
from .library_service import LibraryExtractionError, extract_library
from .virtual_nml_catalog import NmlTrackMetadata
from .models import (
    LibraryDatabase, LibraryKind, LibrarySource, LibrarySourceType, UsbIdentity,
    UsbSession,
)
from .source_write_guard import (
    SourceWriteBoundary, SourceWriteBoundaryError, WriteIntent, WritePlan,
    _parts, _same_object,
)
from .usb_nml_publication import UsbNmlPublicationConfig, UsbNmlPublicationService
from .vhdx_ownership import VhdxOwnershipLedger, VhdxOwnershipState
from .virtual_projection_builder import build_projection_snapshot
from .virtual_projection import VirtualTrack
from .volume_access import TrackOrigin, VolumeIdentity, resolve_mounted_track_origin
from .windows_diskpart_virtual_disk_backend import (
    SubprocessDiskpartRunner,
    detect_windows_virtual_disk_capabilities,
    select_windows_virtual_disk_backend,
)
from .windows_drives import (
    DriveInfo,
    PreflightStatus,
    RekordboxStructureKind,
    list_windows_volumes,
    preflight_rekordbox_volumes,
)
from .windows_vhdx_nml_adapter import (
    WindowsVhdxNmlVolumeAdapter,
    WindowsVhdxNmlVolumeConfig,
    WindowsVhdxIncompatibleError,
    WindowsVirtualDiskBackend,
)
from .windows_virtual_disk_backend import (
    SubprocessPowerShellRunner, WindowsStorageElevationRequiredError,
)


class PublicRuntimeError(RuntimeError):
    """Sanitized failure with a stable public category."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_DISK_OPERATIONS = threading.RLock()


@contextmanager
def _disk_operation_guard():
    """Serialize managed disk effects across threads and local Windows processes."""
    with _DISK_OPERATIONS:
        if sys.platform != "win32":
            yield
            return
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.CreateMutexW(None, False, "Local\\TRAKFROM_BX_STORAGE_OPERATIONS")
        if not handle:
            raise PublicRuntimeError("STORAGE_LOCK_FAILURE")
        acquired = False
        try:
            result = kernel.WaitForSingleObject(handle, 30000)
            acquired = result in (0, 0x80)
            if not acquired:
                raise PublicRuntimeError("STORAGE_BUSY")
            if result == 0x80:
                raise PublicRuntimeError("STORAGE_RECOVERY_REQUIRED")
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


def _serialized(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        with _disk_operation_guard():
            return operation(*args, **kwargs)
    return call


def _caused_by(error: BaseException, expected: type[BaseException]) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, expected):
            return True
        current = current.__cause__
    return False


@dataclass(frozen=True, slots=True)
class PublicPreflight:
    ready: bool
    code: str
    source_id: str | None = None


@dataclass(frozen=True, slots=True)
class PublicPublication:
    source_id: str
    playlist_count: int
    track_count: int
    changed: bool
    mount_point: Path | None


@dataclass(frozen=True, slots=True)
class PublicStatus:
    source_id: str | None
    state: str
    attached: bool = False


@dataclass(frozen=True, slots=True)
class _DetachAuthorization:
    backend: WindowsVirtualDiskBackend
    path: Path
    generation: str


class _LegacyReader:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root

    def read(self, session: UsbSession):
        registry = LibraryProviderRegistry(
            (LegacyServiceProvider(session.session_id, Path(session.root)),)
        )
        library, _ = extract_library(
            session, self.data_root, provider_registry=registry,
            persist_manifest=False,
        )
        return library


class _ProjectionBuilder:
    def build(self, library, *, volume_identity, source_id, display_name, snapshot_id):
        input_data = build_projection_input_from_library(
            library, volume_identity=volume_identity, source_id=source_id,
            display_name=display_name, snapshot_id=snapshot_id,
        )
        return build_projection_snapshot(input_data)


class _LibraryMetadataResolver:
    """Resolve only metadata belonging to the exact projected library origin."""

    def __init__(self, library: UnifiedLibrary) -> None:
        self._metadata = {
            (library.source, str(track.track_id), track.source_path): NmlTrackMetadata(
                title=track.title,
                artist=track.artist,
                # Rekordbox remains the authority before native host analysis.
                bpm=track.raw_bpm if track.bpm_source == "rekordbox" else track.bpm,
                key=track.active_key,
                display_key=(track.active_key if track.traktor_analysis_key
                             else track.rekordbox_key_original),
                duration_seconds=track.duration_seconds,
                position=track.track_number,
            )
            for track in library.tracks
        }

    def resolve(self, track: VirtualTrack) -> NmlTrackMetadata | None:
        origin = track.origin
        return self._metadata.get(
            (origin.provider_id, origin.native_library_id, origin.virtual_path.value)
        )


class _LibraryMetadataResolverFactory:
    def build(self, library: UnifiedLibrary) -> _LibraryMetadataResolver:
        return _LibraryMetadataResolver(library)


class _MountedPathResolver:
    def __init__(self, identity: VolumeIdentity, root: Path) -> None:
        self.identity = identity
        self.root = root

    def resolve(self, origin: TrackOrigin) -> str:
        return str(resolve_mounted_track_origin(origin, self.identity, self.root).local_path)


def _default_backend(storage_root: Path) -> WindowsVirtualDiskBackend:
    selected = select_windows_virtual_disk_backend(
        storage_root,
        power_shell_runner=SubprocessPowerShellRunner(),
        diskpart_runner=SubprocessDiskpartRunner(),
    )
    return selected.backend


def _default_capability_probe() -> bool:
    capabilities = detect_windows_virtual_disk_capabilities(
        SubprocessPowerShellRunner(), SubprocessDiskpartRunner(),
    )
    return capabilities.hyper_v_available or capabilities.diskpart_available


class PublicRuntime:
    """One explicit operation per call; no watcher or private package route."""

    def __init__(
        self,
        *,
        volumes: Callable[[], Iterable[DriveInfo]] = list_windows_volumes,
        backend_factory: Callable[[Path], WindowsVirtualDiskBackend] = _default_backend,
        capability_probe: Callable[[], bool] | None = None,
        host_probe: HostProcessProbe | None = None,
        require_windows: bool = True,
    ) -> None:
        self._volumes = volumes
        self._backend_factory = backend_factory
        self._capability_probe = capability_probe or (
            _default_capability_probe if backend_factory is _default_backend else lambda: True
        )
        self._host_probe = host_probe or WindowsTraktorProcessProbe()
        self._require_windows = require_windows

    @staticmethod
    def _source_id(serial: str) -> tuple[str, str]:
        identity_id = hashlib.blake2s(serial.upper().encode(), digest_size=16).hexdigest()
        return identity_id, f"rekordbox-usb:{identity_id}"

    @staticmethod
    def _paths(data_root: Path, identity_id: str) -> tuple[Path, Path]:
        storage_root = data_root / "managed"
        return storage_root, storage_root / f"{identity_id}.vhdx"

    def _selected_volume(self, volume_root: Path) -> DriveInfo | None:
        requested = os.path.normcase(os.path.abspath(volume_root))
        matches = [
            item for item in self._volumes()
            if os.path.normcase(os.path.abspath(item.root)) == requested
        ]
        return matches[0] if len(matches) == 1 else None

    def resolve_source(self, volume_root: Path | None, data_root: Path) -> Path:
        """Resolve a unique source using the existing shallow discovery boundary."""
        if volume_root is not None:
            return volume_root
        if self._require_windows and sys.platform != "win32":
            raise PublicRuntimeError("UNSUPPORTED_PLATFORM")
        if not data_root.is_absolute():
            raise PublicRuntimeError("INVALID_PATH")
        try:
            eligible = []
            for volume in self._volumes():
                # Fixed USB devices need positive USB bus evidence. Never infer USB
                # from a Rekordbox marker on a local, network, or virtual disk.
                if not self.eligible_volume(volume, data_root):
                    continue
                if not volume.serial or not volume.root.is_absolute() or not volume.root.is_dir():
                    continue
                root = volume.root.resolve(strict=True)
                destination = data_root.resolve()
                if root == destination or root.is_relative_to(destination):
                    continue
                eligible.append(volume)
            candidates = [
                result.volume for result in preflight_rekordbox_volumes(eligible)
                if result.status in (PreflightStatus.READY, PreflightStatus.READY_WITH_WARNINGS)
                and sum(item.kind is RekordboxStructureKind.DEVICE_LIBRARY
                        for item in result.evidence) == 1
            ]
        except Exception as exc:
            raise PublicRuntimeError("SOURCE_DISCOVERY_FAILURE") from exc
        if not candidates:
            raise PublicRuntimeError("NO_REKORDBOX_USB")
        if len(candidates) != 1:
            raise PublicRuntimeError("MULTIPLE_REKORDBOX_USB")
        return candidates[0].root

    @staticmethod
    def eligible_volume(volume: DriveInfo, data_root: Path) -> bool:
        """Metadata eligibility, shared by manual selection and continuous watch."""
        if volume.drive_type not in (2, 3) or volume.bus_type in (14, 15):
            return False
        if volume.drive_type == 3 and volume.bus_type != 7:
            return False
        if (volume.label or "").upper().startswith(("TRAKFROM_BX", "TRAKTORBOX")):
            return False
        root = os.path.normcase(os.path.abspath(volume.root))
        destination = os.path.normcase(os.path.abspath(data_root))
        return root != destination and not root.startswith(destination.rstrip(os.sep) + os.sep)

    def preflight(
        self, volume_root: Path, data_root: Path, *, verify_library: bool = True,
    ) -> PublicPreflight:
        """Observe inputs and tool availability without creating output state."""
        if self._require_windows and sys.platform != "win32":
            return PublicPreflight(False, "UNSUPPORTED_PLATFORM")
        if not volume_root.is_absolute() or not data_root.is_absolute():
            return PublicPreflight(False, "INVALID_PATH")
        try:
            volume = self._selected_volume(volume_root)
            if volume is None or not volume.root.is_dir() or not volume.serial:
                return PublicPreflight(False, "SOURCE_UNAVAILABLE")
            classified = preflight_rekordbox_volumes((volume,))[0]
            legacy = [
                item for item in classified.evidence
                if item.kind is RekordboxStructureKind.DEVICE_LIBRARY
            ]
            if classified.status not in (PreflightStatus.READY, PreflightStatus.READY_WITH_WARNINGS) or len(legacy) != 1:
                return PublicPreflight(False, "UNSUPPORTED_LIBRARY")
            if data_root.exists() and (not data_root.is_dir() or data_root.is_symlink()):
                return PublicPreflight(False, "INVALID_DATA_ROOT")
            identity_id, source_id = self._source_id(volume.serial)
            storage_root, vhdx_path = self._paths(data_root, identity_id)
            try:
                boundary = SourceWriteBoundary(volume.root)
                permit = boundary.authorize(
                    WritePlan(WriteIntent.VHDX, (data_root, storage_root, vhdx_path))
                )
                permit.close()
            except SourceWriteBoundaryError:
                return PublicPreflight(False, "SOURCE_SAFETY_BLOCK")
            if storage_root.exists() and (not storage_root.is_dir() or storage_root.is_symlink()):
                return PublicPreflight(False, "VHDX_PRECONDITION_FAILURE")
            if verify_library:
                try:
                    _LegacyReader(data_root).read(self._session(volume))
                except LibraryExtractionError as exc:
                    return PublicPreflight(False, self._library_failure(exc))
            try:
                capable = self._capability_probe()
            except Exception as exc:
                code = "ELEVATION_REQUIRED" if _caused_by(exc, WindowsStorageElevationRequiredError) else "VHDX_PRECONDITION_FAILURE"
                return PublicPreflight(False, code)
            if not capable:
                return PublicPreflight(False, "VHDX_PRECONDITION_FAILURE")
            if vhdx_path.exists():
                try:
                    self._owned_record(source_id, storage_root, vhdx_path)
                except PublicRuntimeError as exc:
                    return PublicPreflight(False, exc.code)
            return PublicPreflight(True, "READY", source_id)
        except Exception:
            return PublicPreflight(False, "PREFLIGHT_FAILURE")

    def _ready_volume(
        self, volume_root: Path, data_root: Path, *, verify_library: bool = True,
    ) -> DriveInfo:
        result = self.preflight(volume_root, data_root, verify_library=verify_library)
        if not result.ready:
            raise PublicRuntimeError(result.code)
        volume = self._selected_volume(volume_root)
        if volume is None or not volume.serial:
            raise PublicRuntimeError("SOURCE_UNAVAILABLE")
        return volume

    def _backend(self, storage_root: Path) -> WindowsVirtualDiskBackend:
        try:
            return self._backend_factory(storage_root)
        except Exception as exc:
            code = "ELEVATION_REQUIRED" if _caused_by(exc, WindowsStorageElevationRequiredError) else "VHDX_PRECONDITION_FAILURE"
            raise PublicRuntimeError(code) from exc

    @staticmethod
    def _library_failure(error: LibraryExtractionError) -> str:
        if isinstance(error.__cause__, CompatibilityFailure):
            return {
                "UNSUPPORTED": "UNSUPPORTED_LIBRARY",
                "INVALID_LIBRARY": "INVALID_LIBRARY",
                "READ_ERROR": "READ_ERROR",
                "PROVIDER_UNAVAILABLE": "PROVIDER_UNAVAILABLE",
            }.get(error.__cause__.status, "INVALID_LIBRARY")
        return "UNSUPPORTED_LIBRARY" if error.stage == "resolve_library_provider" else "INVALID_LIBRARY"

    def _host_clear(self) -> None:
        try:
            state = self._host_probe.state()
        except Exception as exc:
            raise PublicRuntimeError("HOST_STATE_UNKNOWN") from exc
        if state is not HostProcessState.NOT_RUNNING:
            raise PublicRuntimeError("HOST_NOT_CLEAR")

    @staticmethod
    def _session(volume: DriveInfo) -> UsbSession:
        """Build only the shallow, selected library inventory; never scan audio."""
        if not volume.serial:
            raise PublicRuntimeError("SOURCE_UNAVAILABLE")
        detection = preflight_rekordbox_volumes((volume,))[0]
        libraries = [
            LibraryDatabase(
                LibraryKind.DEVICE_LIBRARY if item.kind is RekordboxStructureKind.DEVICE_LIBRARY
                else LibraryKind.ONE_LIBRARY,
                str(item.path), item.path.stat().st_size,
            )
            for item in detection.evidence
        ]
        seed = f"{volume.serial}|{volume.root}".encode("utf-8")
        return UsbSession(
            session_id=hashlib.blake2s(seed, digest_size=8).hexdigest(),
            root=str(volume.root), volume_label=volume.label,
            volume_serial=volume.serial, filesystem=volume.filesystem,
            libraries=libraries,
        )

    @staticmethod
    def _config(storage_root: Path, vhdx_path: Path) -> WindowsVhdxNmlVolumeConfig:
        return WindowsVhdxNmlVolumeConfig(
            storage_root, vhdx_path, 64, "TRAKFROM_BX",
            remove_vhdx_on_unpublish=False,
        )

    @_serialized
    def publish(
        self, volume_root: Path, data_root: Path, *, allow_running_host: bool = False,
        expected_serial: str | None = None,
    ) -> PublicPublication:
        volume = self._ready_volume(volume_root, data_root)
        if expected_serial is not None and volume.serial != expected_serial:
            raise PublicRuntimeError("SOURCE_IDENTITY_CHANGED")
        assert volume.serial is not None
        identity_id, source_id = self._source_id(volume.serial)
        storage_root, vhdx_path = self._paths(data_root, identity_id)
        if allow_running_host and not vhdx_path.exists():
            try:
                state = self._host_probe.state()
            except Exception as exc:
                raise PublicRuntimeError("HOST_STATE_UNKNOWN") from exc
            if state not in (HostProcessState.RUNNING, HostProcessState.NOT_RUNNING):
                raise PublicRuntimeError("HOST_STATE_UNKNOWN")
        else:
            self._host_clear()
        try:
            boundary = SourceWriteBoundary(volume.root)
            permit = boundary.authorize(WritePlan(WriteIntent.VHDX, (data_root, storage_root)))
            try:
                if not data_root.exists():
                    permit.revalidate_before_side_effect(data_root)
                    data_root.mkdir()
                    permit.record_created(data_root)
                if not storage_root.exists():
                    permit.revalidate_before_side_effect(storage_root)
                    storage_root.mkdir()
                    permit.record_created(storage_root)
            finally:
                permit.close()
            session = self._session(volume)
            legacy = next(
                item for item in session.libraries
                if Path(item.path) == volume.root / "PIONEER" / "rekordbox" / "export.pdb"
            )
            hasher = hashlib.sha256()
            with Path(legacy.path).open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
            identity = UsbIdentity(
                identity_id, volume.serial, volume.label, session.root,
                None, legacy.path, digest, "", "", True,
            )
            source = LibrarySource(
                source_id, LibrarySourceType.REKORDBOX_USB,
                volume.label or "Rekordbox USB", identity_id,
            )
            adapter = WindowsVhdxNmlVolumeAdapter(
                self._config(storage_root, vhdx_path), self._backend(storage_root),
                source_root=volume.root,
            )
            service = UsbNmlPublicationService(
                _LegacyReader(data_root), _ProjectionBuilder(),
                path_resolver=_MountedPathResolver(VolumeIdentity(identity_id), volume.root),
                publisher=adapter,
                metadata_resolver_factory=_LibraryMetadataResolverFactory(),
            )
            result = service.publish(session, UsbNmlPublicationConfig(identity, source, digest))
            return PublicPublication(
                result.source_id, result.playlist_count, result.track_count,
                result.changed, result.mount_point,
            )
        except PublicRuntimeError:
            raise
        except SourceWriteBoundaryError as exc:
            raise PublicRuntimeError("SOURCE_SAFETY_BLOCK") from exc
        except LibraryExtractionError as exc:
            raise PublicRuntimeError(self._library_failure(exc)) from exc
        except WindowsVhdxIncompatibleError as exc:
            raise PublicRuntimeError("VHDX_PRECONDITION_FAILURE") from exc
        except Exception as exc:
            code = "ELEVATION_REQUIRED" if _caused_by(exc, WindowsStorageElevationRequiredError) else "PUBLICATION_FAILURE"
            raise PublicRuntimeError(code) from exc

    def _owned_inspection(
        self, volume: DriveInfo, data_root: Path,
        *, backend: WindowsVirtualDiskBackend | None = None,
    ):
        assert volume.serial is not None
        identity_id, source_id = self._source_id(volume.serial)
        storage_root, vhdx_path = self._paths(data_root, identity_id)
        if not vhdx_path.exists():
            return source_id, storage_root, vhdx_path, None, None, None
        if not vhdx_path.is_file() or vhdx_path.is_symlink():
            raise PublicRuntimeError("VHDX_PRECONDITION_FAILURE")
        backend = backend or self._backend(storage_root)
        inspection = backend.inspect(vhdx_path)
        if inspection.vhdx_path != vhdx_path or not inspection.exists:
            raise PublicRuntimeError("VHDX_PRECONDITION_FAILURE")
        ledger, _ = self._owned_record(source_id, storage_root, vhdx_path)
        observed = ledger.status(vhdx_path, inspection.physical_identity)
        if observed not in (
            VhdxOwnershipState.EXPOSED,
            VhdxOwnershipState.MANAGED_ATTACHED,
            VhdxOwnershipState.MANAGED_DETACHED,
            VhdxOwnershipState.OWNED_VALID,
        ):
            raise PublicRuntimeError("OWNERSHIP_FAILURE")
        return source_id, storage_root, vhdx_path, backend, inspection, ledger

    @staticmethod
    def _owned_record(source_id: str, storage_root: Path, vhdx_path: Path):
        if not vhdx_path.is_file() or vhdx_path.is_symlink():
            raise PublicRuntimeError("VHDX_PRECONDITION_FAILURE")
        ledger = VhdxOwnershipLedger(storage_root)
        try:
            record = ledger.record(vhdx_path)
            if (
                record is None or record.source_id != source_id
                or record.path != vhdx_path
                or not record.generation or not record.operation_id
            ):
                raise PublicRuntimeError("OWNERSHIP_FAILURE")
            observed = ledger.status(vhdx_path, ledger.observe(vhdx_path))
        except PublicRuntimeError:
            raise
        except Exception as exc:
            raise PublicRuntimeError("OWNERSHIP_FAILURE") from exc
        if observed not in (
            VhdxOwnershipState.EXPOSED, VhdxOwnershipState.MANAGED_ATTACHED,
            VhdxOwnershipState.MANAGED_DETACHED, VhdxOwnershipState.OWNED_VALID,
        ):
            raise PublicRuntimeError("OWNERSHIP_FAILURE")
        return ledger, record

    def _offline_inspection(self, volume_root: Path, data_root: Path):
        if (not volume_root.is_absolute() or not data_root.is_absolute()
                or volume_root.exists() or not data_root.is_dir() or data_root.is_symlink()):
            raise PublicRuntimeError("SOURCE_UNAVAILABLE")
        storage_root = data_root / "managed"
        if not storage_root.is_dir() or storage_root.is_symlink():
            raise PublicRuntimeError("SOURCE_UNAVAILABLE")
        try:
            records = VhdxOwnershipLedger(storage_root).records()
        except Exception as exc:
            raise PublicRuntimeError("OWNERSHIP_FAILURE") from exc
        requested = os.path.normcase(os.path.abspath(volume_root))
        matches = [
            record for record in records
            if record.write_binding is not None
            and os.path.normcase(os.path.abspath(record.write_binding.source_root)) == requested
        ]
        if len(matches) != 1:
            raise PublicRuntimeError("SOURCE_UNAVAILABLE" if not matches else "OWNERSHIP_FAILURE")
        record = matches[0]
        prefix = "rekordbox-usb:"
        if not record.source_id.startswith(prefix):
            raise PublicRuntimeError("OWNERSHIP_FAILURE")
        identity_id = record.source_id[len(prefix):]
        expected = self._paths(data_root, identity_id)[1]
        if (
            len(identity_id) != 32 or any(char not in "0123456789abcdef" for char in identity_id)
            or record.path != expected or record.write_binding.destination != expected
        ):
            raise PublicRuntimeError("OWNERSHIP_FAILURE")
        return self._inspect_path(record.source_id, storage_root, expected)

    def _inspect_path(self, source_id: str, storage_root: Path, vhdx_path: Path):
        if not vhdx_path.exists():
            return source_id, storage_root, vhdx_path, None, None, None
        self._owned_record(source_id, storage_root, vhdx_path)
        backend = self._backend(storage_root)
        inspection = backend.inspect(vhdx_path)
        if inspection.vhdx_path != vhdx_path or not inspection.exists:
            raise PublicRuntimeError("VHDX_PRECONDITION_FAILURE")
        ledger = VhdxOwnershipLedger(storage_root)
        if ledger.status(vhdx_path, inspection.physical_identity) not in (
            VhdxOwnershipState.EXPOSED, VhdxOwnershipState.MANAGED_ATTACHED,
            VhdxOwnershipState.MANAGED_DETACHED, VhdxOwnershipState.OWNED_VALID,
        ):
            raise PublicRuntimeError("OWNERSHIP_FAILURE")
        return source_id, storage_root, vhdx_path, backend, inspection, ledger

    def _lifecycle_inspection(self, volume_root: Path, data_root: Path):
        if not volume_root.exists():
            return self._offline_inspection(volume_root, data_root), None
        volume = self._ready_volume(volume_root, data_root, verify_library=False)
        return self._owned_inspection(volume, data_root), volume.root

    def status(self, volume_root: Path, data_root: Path) -> PublicStatus:
        try:
            result, _ = self._lifecycle_inspection(volume_root, data_root)
            source_id, _, _, _, inspection, _ = result
            if inspection is None:
                return PublicStatus(source_id, "NO_PUBLICATION")
            return PublicStatus(source_id, "ACTIVE" if inspection.attached else "DETACHED", inspection.attached)
        except PublicRuntimeError:
            raise
        except Exception as exc:
            raise PublicRuntimeError("VHDX_PRECONDITION_FAILURE") from exc

    @_serialized
    def unpublish(self, volume_root: Path, data_root: Path) -> PublicStatus:
        self._host_clear()
        try:
            result, source_root = self._lifecycle_inspection(volume_root, data_root)
            source_id, storage_root, vhdx_path, backend, inspection, ledger = result
            if inspection is None:
                return PublicStatus(source_id, "NO_PUBLICATION")
            assert backend is not None and ledger is not None
            ledger, source_root = self._detach_context(source_id, storage_root, vhdx_path, ledger, data_root)
            authorization = ledger.authorize(vhdx_path, inspection.physical_identity, "detach")
            guarded = _DetachAuthorization(backend, vhdx_path, authorization.generation)
            adapter = WindowsVhdxNmlVolumeAdapter(
                self._config(storage_root, vhdx_path), backend,
                source_root=source_root, ownership_ledger=ledger,
            )
            adapter.unpublish_existing(detach_authorization=guarded)
            return PublicStatus(source_id, "DETACHED")
        except PublicRuntimeError:
            raise
        except Exception as exc:
            code = "ELEVATION_REQUIRED" if _caused_by(exc, WindowsStorageElevationRequiredError) else "UNPUBLISH_FAILURE"
            raise PublicRuntimeError(code) from exc

    def inspect_managed(self, source_id: str, vhdx_path: Path, data_root: Path):
        """Inspect an exact owned path independently of a reused drive letter."""
        prefix = "rekordbox-usb:"
        identity_id = source_id.removeprefix(prefix)
        if (not source_id.startswith(prefix) or len(identity_id) != 32
                or any(char not in "0123456789abcdef" for char in identity_id)
                or vhdx_path != self._paths(data_root, identity_id)[1]):
            raise PublicRuntimeError("OWNERSHIP_FAILURE")
        return self._inspect_path(source_id, data_root / "managed", vhdx_path)

    def _detach_context(self, source_id, storage, vhdx_path, ledger, data_root):
        """Reprove safety from a present, identical USB after NT path renumbering.

        The persisted offline boundary stays strict. A fresh boundary is allowed
        only with the same source object AND the same local parent object/path;
        the exact VHDX identity/generation was independently validated above.
        """
        record = ledger.record(vhdx_path)
        binding = record.write_binding if record else None
        if binding is None:
            raise PublicRuntimeError("OWNERSHIP_FAILURE")
        matches = [volume for volume in self._volumes() if volume.serial
                   and self._source_id(volume.serial)[1] == source_id
                   and self.eligible_volume(volume, data_root) and volume.root.is_dir()]
        if not matches:
            return ledger, None
        if len(matches) != 1:
            raise PublicRuntimeError("AMBIGUOUS_SOURCE_IDENTITY")
        fresh = SourceWriteBoundary(matches[0].root)
        current = fresh.capture_local_destination(vhdx_path, intent=WriteIntent.VHDX)
        if (not _same_object(current.source_identity, binding.source_identity)
                or not _same_object(current.parent_identity, binding.parent_identity)
                or _parts(current.parent_identity.final_path) != binding.parent_parts
                or binding.destination != vhdx_path):
            raise PublicRuntimeError("SOURCE_SAFETY_BLOCK")
        return VhdxOwnershipLedger(storage, boundary=fresh), matches[0].root

    @_serialized
    def detach_managed(self, source_id: str, vhdx_path: Path, data_root: Path):
        """Detach a positively owned reference; never resolve by current drive letter."""
        self._host_clear()
        _, storage, _, backend, inspection, ledger = self.inspect_managed(source_id, vhdx_path, data_root)
        if inspection is None or backend is None or ledger is None:
            raise PublicRuntimeError("OWNERSHIP_FAILURE")
        try:
            ledger, source_root = self._detach_context(source_id, storage, vhdx_path, ledger, data_root)
            authorization = ledger.authorize(vhdx_path, inspection.physical_identity, "detach")
            adapter = WindowsVhdxNmlVolumeAdapter(
                self._config(storage, vhdx_path), backend, ownership_ledger=ledger,
                source_root=source_root,
            )
            return adapter.unpublish_existing(
                detach_authorization=_DetachAuthorization(backend, vhdx_path, authorization.generation)
            )
        except PublicRuntimeError:
            raise
        except Exception as exc:
            raise PublicRuntimeError("VHDX_PRECONDITION_FAILURE") from exc
