"""Public composition of the existing foreground multi-source watcher."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import sys
from typing import Callable

from .host_process import HostProcessState
from .public_runtime import PublicRuntime, PublicRuntimeError
from .usb_watch import (
    ActiveUsbPublication, PollingWindowsVolumeObserver, StartupRecoveryDeferred,
    StartupRecoveryFailure, StartupRecoveryResult, StartupRecoveryUnpublication,
    WatchUsbConfig, WatchUsbEvent, WatchUsbRuntime, volume_key,
)
from .vhdx_ownership import VhdxOwnershipLedger
from .windows_drives import (
    PreflightStatus, build_rekordbox_preflight_report, preflight_rekordbox_volumes,
)


@dataclass(frozen=True, slots=True)
class WatchPublicationResult:
    identity_id: str
    source_id: str
    vhdx_path: Path
    vhdx_mount: Path | None
    changed: bool
    status: str = "PUBLISHED"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WatchDetachResult:
    identity_id: str
    source_id: str
    vhdx_path: Path
    vhdx_mount_before: Path | None
    vhdx_mount_after: Path | None
    changed: bool


@contextmanager
def exclusive_runtime(data_root: Path):
    """One continuous storage manager per data root, without creating lock files."""
    if sys.platform != "win32":
        yield
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    key = hashlib.sha256(str(data_root.resolve()).casefold().encode()).hexdigest()
    handle = kernel.CreateMutexW(None, False, "Local\\TRAKFROM_BX_" + key)
    if not handle:
        raise PublicRuntimeError("RUNTIME_LOCK_FAILURE")
    already_running = ctypes.get_last_error() == 183
    # Shared activity marker: both launcher and worker may hold it. Setup only
    # observes existence and refuses maintenance; no process is terminated.
    activity = kernel.CreateMutexW(None, False, "Global\\TRAKFROM_BX_ACTIVITY_V1")
    if not activity:
        kernel.CloseHandle(handle)
        raise PublicRuntimeError("RUNTIME_LOCK_FAILURE")
    try:
        if already_running:
            raise PublicRuntimeError("RUNTIME_ALREADY_RUNNING")
        yield
    finally:
        kernel.CloseHandle(activity)
        kernel.CloseHandle(handle)


class PublicWatchRuntime:
    """Serial foreground lifecycle; each USB retains its own owned VHDX."""

    def __init__(self, runtime: PublicRuntime, data_root: Path, *,
                 event_sink: Callable[[WatchUsbEvent], None], observer=None,
                 config: WatchUsbConfig | None = None, **watch_options):
        self.runtime = runtime
        self.data_root = data_root
        self._sink = event_sink
        self.sources: dict[str, dict[str, object]] = {}
        self._source_by_volume: dict[str, str] = {}
        self._preflight_serials: dict[str, str | None] = {}
        self.watcher = WatchUsbRuntime(
            observer=observer or PollingWindowsVolumeObserver(runtime._volumes),
            preflight_runner=self._preflight, publisher=self._publish,
            unpublisher=self._detach, shutdown_unpublisher=self._detach,
            startup_recoverer=self._recover, host_process_probe=runtime._host_probe,
            eligible_volume=lambda volume: runtime.eligible_volume(volume, data_root),
            config=config or WatchUsbConfig(publish_existing=True, retry_seconds=5.0),
            event_sink=self._event,
            error_details=lambda exc: {"code": getattr(exc, "code", "OPERATION_FAILURE")},
            **watch_options,
        )

    def _event(self, event: WatchUsbEvent) -> None:
        diagnostic = event.details.get("diagnostic", {})
        if (event.event == "VOLUME_ERROR" and event.volume is not None and isinstance(diagnostic, dict)
                and diagnostic.get("code") == "HOST_NOT_CLEAR"):
            try:
                running = self.runtime._host_probe.state() is HostProcessState.RUNNING
            except Exception:
                running = False
            if running:
                event = replace(event, event="USB_PUBLICATION_DEFERRED", details={
                    **event.details,
                    "source_id": self._source_by_volume.get(volume_key(event.volume)),
                    "reason": "TRAKTOR_RUNNING",
                })
        states = {
            "VOLUME_ARRIVED": "DETECTED", "USB_PUBLICATION_STARTED": "PUBLISHING",
            "USB_PUBLISHED": "PUBLISHED", "MANAGED_VHDX_RECONCILED": "PUBLISHED",
            "USB_PUBLICATION_DEFERRED": "DEFERRED",
            "VOLUME_REMOVED": "REMOVED", "USB_UNPUBLICATION_STARTED": "UNPUBLISHING",
            "USB_UNPUBLISHED": "REMOVED", "SHUTDOWN_UNPUBLISHED": "REMOVED",
            "USB_UNPUBLICATION_DEFERRED": "DEFERRED", "STARTUP_RECOVERY_DEFERRED": "DEFERRED",
            "USB_SOURCE_RELOCATION_DEFERRED": "DEFERRED", "SHUTDOWN_CLEANUP_DEFERRED": "DEFERRED",
            "USB_SOURCE_RESTORED": "PUBLISHED", "VOLUME_ERROR": "ERROR",
            "SOURCE_PREFLIGHT": "PREFLIGHT",
            "SOURCE_PREFLIGHT_FAILED": "ERROR",
            "USB_UNPUBLICATION_ERROR": "ERROR", "SHUTDOWN_CLEANUP_ERROR": "ERROR",
        }
        if event.event in states:
            source_id = event.details.get("source_id")
            if source_id is None and event.volume:
                source_id = self._source_by_volume.get(volume_key(event.volume))
            if isinstance(source_id, str):
                state = self.sources.setdefault(source_id, {"source_id": source_id})
                state.update(event.details)
                state["state"] = states[event.event]
                if event.volume:
                    state["physical_volume"] = event.volume
                    self._source_by_volume[volume_key(event.volume)] = source_id
        self._sink(event)

    def _preflight(self, *, volume_root):
        volume = self.runtime._selected_volume(Path(volume_root))
        if volume is None:
            return build_rekordbox_preflight_report(())
        self._preflight_serials[volume_key(volume.root)] = volume.serial
        report = build_rekordbox_preflight_report(preflight_rekordbox_volumes((volume,)))
        if volume.serial and sum(
            item.serial is not None and item.serial.upper() == volume.serial.upper()
            and self.runtime.eligible_volume(item, self.data_root)
            for item in self.runtime._volumes()
        ) != 1:
            return replace(report, volumes=(replace(report.volumes[0],
                           status=PreflightStatus.BLOCKED.value, reasons=("AMBIGUOUS_SOURCE_IDENTITY",)),))
        if report.volumes[0].status not in (PreflightStatus.READY, PreflightStatus.READY_WITH_WARNINGS):
            return report
        identity_id, source_id = self.runtime._source_id(volume.serial) if volume.serial else (None, None)
        self._event(WatchUsbEvent("SOURCE_PREFLIGHT", str(volume.root),
                                 {"identity_id": identity_id, "source_id": source_id}))
        result = self.runtime.preflight(volume.root, self.data_root)
        if not result.ready:
            self._event(WatchUsbEvent("SOURCE_PREFLIGHT_FAILED", str(volume.root),
                                     {"source_id": source_id, "code": result.code}))
            item = replace(report.volumes[0], status=PreflightStatus.BLOCKED.value,
                           reasons=(result.code,))
            return replace(report, volumes=(item,))
        return report

    def _publish(self, *, volume_root):
        volume = self.runtime._selected_volume(Path(volume_root))
        if volume is None or not volume.serial:
            raise PublicRuntimeError("SOURCE_UNAVAILABLE")
        if self._preflight_serials.get(volume_key(volume.root)) != volume.serial:
            raise PublicRuntimeError("SOURCE_IDENTITY_CHANGED")
        identity_id, source_id = self.runtime._source_id(volume.serial)
        publication = self.runtime.publish(volume.root, self.data_root, allow_running_host=True,
                                           expected_serial=volume.serial)
        if publication.source_id != source_id:
            raise PublicRuntimeError("SOURCE_IDENTITY_CHANGED")
        return WatchPublicationResult(identity_id, source_id,
                                      self.runtime._paths(self.data_root, identity_id)[1],
                                      publication.mount_point, publication.changed)

    def _detach(self, *, publication):
        result = self.runtime.detach_managed(publication.source_id, publication.vhdx_path, self.data_root)
        return WatchDetachResult(publication.identity_id, publication.source_id, publication.vhdx_path,
                                 result.mount_before, result.mount_after, result.changed)

    def _recover(self, *, volumes, host_state):
        storage = self.data_root / "managed"
        if not storage.exists():
            return StartupRecoveryResult()
        if not storage.is_dir() or storage.is_symlink():
            raise PublicRuntimeError("VHDX_PRECONDITION_FAILURE")
        reconciled, unpublished, errors, deferred = [], [], [], []
        for record in VhdxOwnershipLedger(storage).records():
            try:
                _, _, _, _, inspection, _ = self.runtime.inspect_managed(
                    record.source_id, record.path, self.data_root
                )
                if inspection is None or not inspection.attached:
                    continue
                identity_id = record.source_id.removeprefix("rekordbox-usb:")
                matches = [volume for volume in volumes if volume.serial
                           and self.runtime._source_id(volume.serial)[1] == record.source_id
                           and self.runtime.eligible_volume(volume, self.data_root)]
                binding = record.write_binding
                present = (len(matches) == 1 and binding is not None
                           and volume_key(matches[0].root) == volume_key(binding.source_root))
                if present and host_state is not HostProcessState.NOT_RUNNING:
                    result = self.runtime.preflight(matches[0].root, self.data_root)
                    if result.ready and inspection.mount_point is not None:
                        reconciled.append(ActiveUsbPublication(
                            volume_key(matches[0].root), str(matches[0].root), identity_id,
                            record.source_id, record.path, inspection.mount_point,
                        ))
                        continue
                if host_state is not HostProcessState.NOT_RUNNING:
                    deferred.append(StartupRecoveryDeferred(
                        identity_id, record.source_id, record.path, inspection.mount_point,
                        "TRAKTOR_RUNNING" if host_state is HostProcessState.RUNNING else "HOST_STATE_UNKNOWN",
                    ))
                    continue
                result = self.runtime.detach_managed(record.source_id, record.path, self.data_root)
                unpublished.append(StartupRecoveryUnpublication(
                    identity_id, record.source_id, record.path,
                    result.mount_before, result.mount_after, result.changed,
                ))
            except Exception as exc:
                errors.append(StartupRecoveryFailure(record.path, getattr(exc, "code", "RECOVERY_FAILURE"),
                                                     "Managed source recovery failed safely."))
        return StartupRecoveryResult(tuple(reconciled), tuple(unpublished), tuple(errors), tuple(deferred))

    def run(self, *, max_cycles: int | None = None) -> int:
        if self.runtime._require_windows and sys.platform != "win32":
            raise PublicRuntimeError("UNSUPPORTED_PLATFORM")
        if not self.data_root.is_absolute() or (self.data_root.exists() and
                (not self.data_root.is_dir() or self.data_root.is_symlink())):
            raise PublicRuntimeError("INVALID_DATA_ROOT")
        with exclusive_runtime(self.data_root):
            return self.watcher.run(max_cycles=max_cycles)
