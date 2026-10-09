from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Protocol


class RekordboxStructureKind(StrEnum):
    """Known Rekordbox library marker categories."""

    DEVICE_LIBRARY = "device_library"
    ONE_LIBRARY = "one_library"


class PreflightStatus(StrEnum):
    """Structural compatibility state for a volume before library reading."""

    READY = "ready"
    READY_WITH_WARNINGS = "ready_with_warnings"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


class PreflightReason(StrEnum):
    """Stable reason codes emitted by structural compatibility preflight."""

    NO_LIBRARY_FOUND = "no_library_found"
    ACCESS_ERROR = "access_error"
    PARTIAL_DETECTION = "partial_detection"


@dataclass(frozen=True, slots=True)
class RekordboxStructureMarker:
    """A library marker allowed to be checked relative to a volume root."""

    kind: RekordboxStructureKind
    relative_path: Path


REKORDBOX_LIBRARY_MARKERS = (
    RekordboxStructureMarker(
        RekordboxStructureKind.DEVICE_LIBRARY,
        Path("PIONEER") / "rekordbox" / "export.pdb",
    ),
    RekordboxStructureMarker(
        RekordboxStructureKind.ONE_LIBRARY,
        Path("PIONEER") / "rekordbox" / "exportLibrary.db",
    ),
    RekordboxStructureMarker(
        RekordboxStructureKind.ONE_LIBRARY,
        Path("PIONEER") / "rekordboxAgent" / "exportLibrary.db",
    ),
)
PREFLIGHT_REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class DriveInfo:
    """Neutral metadata for a volume currently mounted by Windows."""

    root: Path
    label: str | None = None
    serial: str | None = None
    filesystem: str | None = None
    drive_type: int | None = None
    bus_type: int | None = None


@dataclass(frozen=True, slots=True)
class RekordboxStructureEvidence:
    """A known Rekordbox marker found by a shallow volume-root check."""

    volume: DriveInfo
    kind: RekordboxStructureKind
    path: Path


@dataclass(frozen=True, slots=True)
class RekordboxVolumeDetection:
    """Shallow Rekordbox detection result for one Windows volume."""

    volume: DriveInfo
    evidence: tuple[RekordboxStructureEvidence, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RekordboxPreflightResult:
    """Compatibility classification for one volume based on shallow evidence."""

    volume: DriveInfo
    status: PreflightStatus
    warnings: tuple[str, ...]
    reasons: tuple[PreflightReason, ...]
    evidence: tuple[RekordboxStructureEvidence, ...]
    has_rekordbox_evidence: bool


@dataclass(frozen=True, slots=True)
class RekordboxPreflightReportEvidence:
    """JSON-safe representation of one shallow Rekordbox marker."""

    kind: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path}


@dataclass(frozen=True, slots=True)
class RekordboxPreflightReportItem:
    """JSON-safe preflight representation for one Windows volume."""

    root: str
    label: str | None
    serial: str | None
    filesystem: str | None
    drive_type: int | None
    status: str
    warnings: tuple[str, ...]
    reasons: tuple[str, ...]
    evidence: tuple[RekordboxPreflightReportEvidence, ...]
    has_rekordbox_evidence: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "volume": {
                "root": self.root,
                "label": self.label,
                "serial": self.serial,
                "filesystem": self.filesystem,
                "drive_type": self.drive_type,
            },
            "status": self.status,
            "warnings": list(self.warnings),
            "reasons": list(self.reasons),
            "evidence": [item.to_dict() for item in self.evidence],
            "has_rekordbox_evidence": self.has_rekordbox_evidence,
        }


@dataclass(frozen=True, slots=True)
class RekordboxPreflightReport:
    """Versioned, deterministic preflight payload for external consumers."""

    schema_version: int
    volumes: tuple[RekordboxPreflightReportItem, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "volumes": [item.to_dict() for item in self.volumes],
        }


@dataclass(frozen=True, slots=True)
class VolumeMetadata:
    """Shallow metadata returned by Windows for a mounted volume."""

    label: str | None = None
    serial: str | None = None
    filesystem: str | None = None


class WindowsVolumeApi(Protocol):
    """Small read-only Windows API surface used by volume inventory."""

    def logical_drive_mask(self) -> int: ...

    def drive_type(self, root: str) -> int: ...

    def volume_metadata(self, root: str) -> VolumeMetadata: ...


class VolumePathProbe(Protocol):
    """Read-only file-existence probe for paths beneath a mounted volume."""

    def is_file(self, path: Path) -> bool: ...


class _PathVolumeProbe:
    """Default read-only probe backed by pathlib."""

    def is_file(self, path: Path) -> bool:
        return path.is_file()


class _CtypesWindowsVolumeApi:
    """Read-only ctypes adapter for the Windows volume metadata APIs."""

    def __init__(self) -> None:
        import ctypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.windll.kernel32

    def logical_drive_mask(self) -> int:
        return int(self._kernel32.GetLogicalDrives())

    def drive_type(self, root: str) -> int:
        return int(self._kernel32.GetDriveTypeW(root))

    def bus_type(self, root: str) -> int | None:
        """Query storage metadata with zero device access; never mount or write."""
        from ctypes import wintypes

        ctypes = self._ctypes
        create = self._kernel32.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        control = self._kernel32.DeviceIoControl
        control.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        control.restype = wintypes.BOOL
        close = self._kernel32.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        handle = create("\\\\.\\" + root.rstrip("\\"), 0, 3, None, 3, 0, None)
        if handle == wintypes.HANDLE(-1).value:
            return None
        try:
            # StorageDeviceProperty / PropertyStandardQuery; descriptor BusType at byte 28.
            query = ctypes.create_string_buffer(12)
            descriptor = ctypes.create_string_buffer(4096)
            returned = wintypes.DWORD()
            ok = control(handle, 0x002D1400, query, 12, descriptor, 4096,
                         ctypes.byref(returned), None)
            if not ok or returned.value < 32:
                return None
            return int.from_bytes(descriptor.raw[28:32], "little")
        finally:
            close(handle)

    def volume_metadata(self, root: str) -> VolumeMetadata:
        from ctypes import wintypes

        volume_name = self._ctypes.create_unicode_buffer(261)
        filesystem_name = self._ctypes.create_unicode_buffer(261)
        serial_number = wintypes.DWORD()
        max_component = wintypes.DWORD()
        flags = wintypes.DWORD()
        ok = self._kernel32.GetVolumeInformationW(
            root,
            volume_name,
            len(volume_name),
            self._ctypes.byref(serial_number),
            self._ctypes.byref(max_component),
            self._ctypes.byref(flags),
            filesystem_name,
            len(filesystem_name),
        )
        if not ok:
            return VolumeMetadata()
        return VolumeMetadata(
            label=volume_name.value or None,
            serial=f"{serial_number.value:08X}",
            filesystem=filesystem_name.value or None,
        )


def detect_rekordbox_structures(
    volumes: Iterable[DriveInfo],
    probe: VolumePathProbe | None = None,
) -> list[RekordboxVolumeDetection]:
    """Find known Rekordbox markers without opening or interpreting their contents.

    Only the explicit paths in ``REKORDBOX_LIBRARY_MARKERS`` are checked below
    each supplied volume root.  This intentionally does not recurse through a
    volume or read databases, ANLZ, or audio files. An inaccessible marker is
    recorded as a warning and does not prevent checking other markers or volumes.
    """
    active_probe = probe if probe is not None else _PathVolumeProbe()
    detections: list[RekordboxVolumeDetection] = []
    for volume in volumes:
        evidence: list[RekordboxStructureEvidence] = []
        warnings: list[str] = []
        for marker in REKORDBOX_LIBRARY_MARKERS:
            path = volume.root / marker.relative_path
            try:
                marker_found = active_probe.is_file(path)
            except OSError as exc:
                warnings.append(f"Could not inspect Rekordbox marker {path}: {exc}")
                continue
            if marker_found:
                evidence.append(
                    RekordboxStructureEvidence(
                        volume=volume,
                        kind=marker.kind,
                        path=path,
                    )
                )
        detections.append(
            RekordboxVolumeDetection(
                volume=volume,
                evidence=tuple(evidence),
                warnings=tuple(warnings),
            )
        )
    return detections


def classify_rekordbox_detections(
    detections: Iterable[RekordboxVolumeDetection],
) -> list[RekordboxPreflightResult]:
    """Classify shallow detection results without inspecting marker contents."""
    results: list[RekordboxPreflightResult] = []
    for detection in detections:
        has_evidence = bool(detection.evidence)
        if has_evidence and not detection.warnings:
            status = PreflightStatus.READY
            reasons: tuple[PreflightReason, ...] = ()
        elif has_evidence:
            status = PreflightStatus.READY_WITH_WARNINGS
            reasons = (PreflightReason.PARTIAL_DETECTION,)
        elif detection.warnings:
            status = PreflightStatus.BLOCKED
            reasons = (PreflightReason.ACCESS_ERROR,)
        else:
            status = PreflightStatus.UNSUPPORTED
            reasons = (PreflightReason.NO_LIBRARY_FOUND,)
        results.append(
            RekordboxPreflightResult(
                volume=detection.volume,
                status=status,
                warnings=detection.warnings,
                reasons=reasons,
                evidence=detection.evidence,
                has_rekordbox_evidence=has_evidence,
            )
        )
    return results


def preflight_rekordbox_volumes(
    volumes: Iterable[DriveInfo],
    probe: VolumePathProbe | None = None,
) -> list[RekordboxPreflightResult]:
    """Run shallow Rekordbox detection and structural compatibility classification."""
    return classify_rekordbox_detections(detect_rekordbox_structures(volumes, probe))


def build_rekordbox_preflight_report(
    results: Iterable[RekordboxPreflightResult],
) -> RekordboxPreflightReport:
    """Serialize already classified preflight results without reading volumes."""
    volumes: list[RekordboxPreflightReportItem] = []
    for result in results:
        evidence = tuple(
            sorted(
                (
                    RekordboxPreflightReportEvidence(
                        kind=item.kind.value,
                        path=str(item.path),
                    )
                    for item in result.evidence
                ),
                key=lambda item: (item.path.casefold(), item.kind),
            )
        )
        volumes.append(
            RekordboxPreflightReportItem(
                root=str(result.volume.root),
                label=result.volume.label,
                serial=result.volume.serial,
                filesystem=result.volume.filesystem,
                drive_type=result.volume.drive_type,
                status=result.status.value,
                warnings=tuple(sorted(result.warnings)),
                reasons=tuple(sorted(reason.value for reason in result.reasons)),
                evidence=evidence,
                has_rekordbox_evidence=result.has_rekordbox_evidence,
            )
        )
    return RekordboxPreflightReport(
        schema_version=PREFLIGHT_REPORT_SCHEMA_VERSION,
        volumes=tuple(
            sorted(
                volumes,
                key=lambda item: (
                    item.root.casefold(),
                    item.serial or "",
                    item.label or "",
                    item.filesystem or "",
                    item.drive_type if item.drive_type is not None else -1,
                    item.status,
                ),
            )
        ),
    )


def has_rekordbox_library(root: Path, probe: VolumePathProbe | None = None) -> bool:
    """Return whether a root has at least one known Rekordbox library marker."""
    detection = detect_rekordbox_structures([DriveInfo(root=root)], probe)[0]
    return bool(detection.evidence)


def filter_rekordbox_drives(
    drives: Iterable[DriveInfo], probe: VolumePathProbe | None = None
) -> list[DriveInfo]:
    """Filter drive candidates by the actual Rekordbox library markers."""
    return [
        detection.volume
        for detection in detect_rekordbox_structures(drives, probe)
        if detection.evidence
    ]


def list_windows_volumes(api: WindowsVolumeApi | None = None) -> list[DriveInfo]:
    """Return mounted Windows volumes using only shallow read-only metadata APIs.

    This inventory deliberately does not inspect files or identify Rekordbox
    structures.  The optional API adapter makes the Windows boundary testable
    without a mounted device.
    """
    if api is None:
        if os.name != "nt":
            return []
        api = _CtypesWindowsVolumeApi()

    bitmask = api.logical_drive_mask()
    volumes: list[DriveInfo] = []
    for index in range(26):
        if not bitmask & (1 << index):
            continue
        root_text = f"{chr(65 + index)}:\\"
        metadata = api.volume_metadata(root_text)
        volumes.append(
            DriveInfo(
                root=Path(root_text),
                label=metadata.label,
                serial=metadata.serial,
                filesystem=metadata.filesystem,
                drive_type=api.drive_type(root_text),
                bus_type=getattr(api, "bus_type", lambda _: None)(root_text),
            )
        )
    return volumes


def list_windows_drives() -> list[DriveInfo]:
    """Backward-compatible name for the neutral Windows volume inventory."""
    return list_windows_volumes()


def list_removable_drives() -> list[DriveInfo]:
    """Return physical removable drives, kept for watcher compatibility."""
    return [item for item in list_windows_drives() if item.drive_type == 2]


def list_rekordbox_drives() -> list[DriveInfo]:
    """Find Rekordbox media by contents instead of trusting Windows drive type."""
    return filter_rekordbox_drives(list_windows_drives())
