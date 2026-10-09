from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .library_models import LibrarySummary, TrackAnalysisState, TrackAvailability


class LibraryKind(StrEnum):
    DEVICE_LIBRARY = "device_library"
    ONE_LIBRARY = "one_library"


class BridgeState(StrEnum):
    IDLE = "idle"
    USB_DETECTED = "usb_detected"
    SCANNING = "scanning"
    READY_METADATA = "ready_metadata"
    EXTRACTING_LIBRARY = "extracting_library"
    LIBRARY_READY = "library_ready"
    EXPORTING_NML = "exporting_nml"
    NML_READY = "nml_ready"
    CACHING = "caching"
    TRACK_CACHED = "track_cached"
    PREPARING_PLAYLIST = "preparing_playlist"
    PLAYLIST_READY = "playlist_ready"
    USB_REMOVED = "usb_removed"
    ERROR = "error"


class UsbDiscoveryState(StrEnum):
    USB_CONNECTED = "usb_connected"
    SCANNING = "scanning"
    INDEXED = "indexed"
    READY = "ready"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class UsbDiscoveryEventType(StrEnum):
    USB_ATTACHED = "usb_attached"
    USB_DETACHED = "usb_detached"
    LIBRARY_UPDATED = "library_updated"


class TrackSource(StrEnum):
    LOCAL = "local"
    REKORDBOX_USB = "rekordbox_usb"


class LibrarySourceType(StrEnum):
    REKORDBOX_USB = "rekordbox_usb"


class CacheState(StrEnum):
    NOT_REQUIRED = "not_required"
    USB_ONLY = "usb_only"
    CACHING = "caching"
    CACHED = "cached"
    FAILED = "failed"


@dataclass(slots=True)
class LibraryDatabase:
    kind: LibraryKind
    path: str
    size_bytes: int


@dataclass(slots=True)
class UsbSession:
    session_id: str
    root: str
    volume_label: str | None = None
    volume_serial: str | None = None
    filesystem: str | None = None
    libraries: list[LibraryDatabase] = field(default_factory=list)
    anlz_files: int = 0
    audio_files: int = 0
    contents_path: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def library_kinds(self) -> set[LibraryKind]:
        return {item.kind for item in self.libraries}


@dataclass(slots=True)
class UsbIdentity:
    identity_id: str
    volume_serial: str | None
    volume_label: str | None
    mount_point: str
    export_library_path: str | None
    device_library_path: str | None
    library_hash: str
    first_seen: str
    last_seen: str
    connected: bool


@dataclass(slots=True)
class LibrarySource:
    source_id: str
    type: LibrarySourceType
    display_name: str
    usb_identity_id: str


@dataclass(slots=True)
class UsbDiscoveryEvent:
    type: UsbDiscoveryEventType
    root: str
    source: LibrarySource | None = None
    error: str | None = None


@dataclass(slots=True)
class DeckStatus:
    deck: str
    source: TrackSource = TrackSource.LOCAL
    availability: TrackAvailability = TrackAvailability.MISSING_USB
    analysis_state: TrackAnalysisState = TrackAnalysisState.UNKNOWN
    resolved_location: str | None = None
    # Retained only for CACHE_LEGACY compatibility.
    cache_state: CacheState = CacheState.NOT_REQUIRED
    track_id: str | None = None
    title: str | None = None
    source_path: str | None = None
    cached_path: str | None = None
    playing: bool = False


@dataclass(slots=True)
class BridgeStatus:
    state: BridgeState = BridgeState.IDLE
    message: str = "WAITING FOR REKORDBOX USB"
    session: UsbSession | None = None
    library_source: LibrarySource | None = None
    progress: float | None = None
    error: str | None = None
    library: LibrarySummary | None = None
    nml_path: str | None = None
    cache_manifest_path: str | None = None
    cached_tracks: int = 0
    cached_bytes: int = 0
    decks: dict[str, DeckStatus] = field(
        default_factory=lambda: {name: DeckStatus(deck=name) for name in "ABCD"}
    )
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ensure_under_root(path: Path, root: Path) -> Path:
    """Resolve path and reject files outside the active USB root."""
    resolved_path = path.resolve(strict=True)
    resolved_root = root.resolve(strict=True)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Track is outside the active USB root: {resolved_path}") from exc
    return resolved_path
