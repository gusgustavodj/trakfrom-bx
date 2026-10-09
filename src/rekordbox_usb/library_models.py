from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from .key_converter import convert_rekordbox_key
from .metadata_normalization import KeyModel, normalize_bpm, normalize_key


class TrackAvailability(StrEnum):
    AVAILABLE_USB = "available_usb"
    MISSING_USB = "missing_usb"
    CACHE_LEGACY_AVAILABLE = "cache_legacy_available"


class TrackAnalysisState(StrEnum):
    UNKNOWN = "unknown"
    INDEXED_FROM_REKORDBOX = "indexed_from_rekordbox"
    ANALYZED_BY_TRAKTOR = "analyzed_by_traktor"
    FAILED = "failed"


class ActiveKeySource(StrEnum):
    REKORDBOX_IMPORTED = "rekordbox_imported"
    TRAKTOR_ANALYZED = "traktor_analyzed"


@dataclass(slots=True)
class CuePoint:
    kind: str
    time_seconds: float
    hotcue_index: int | None = None
    name: str | None = None
    loop_length_seconds: float | None = None
    color_id: int | None = None
    color_rgb: str | None = None


@dataclass(slots=True)
class BeatGridPoint:
    beat: int
    bpm: float
    time_seconds: float


@dataclass(slots=True)
class TrackAnalysis:
    data_path: str | None = None
    resolved_dat_path: str | None = None
    beat_count: int = 0
    bpm_min: float | None = None
    bpm_max: float | None = None
    first_beat_seconds: float | None = None
    last_beat_seconds: float | None = None
    variable_bpm: bool = False
    hot_cues: list[CuePoint] = field(default_factory=list)
    memory_cues: list[CuePoint] = field(default_factory=list)
    saved_loops: list[CuePoint] = field(default_factory=list)
    beat_grid: list[BeatGridPoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LibraryTrack:
    track_id: int
    title: str
    artist: str | None
    album: str | None
    genre: str | None
    label: str | None
    bpm: float | None
    rekordbox_key: str | None
    duration_seconds: int | None
    track_number: int | None
    rating: int | None
    comment: str | None
    rekordbox_path: str
    source_path: str
    file_name: str | None
    file_size: int | None
    analysis: TrackAnalysis = field(default_factory=TrackAnalysis)
    availability: TrackAvailability = TrackAvailability.MISSING_USB
    analysis_state: TrackAnalysisState = TrackAnalysisState.UNKNOWN
    # Filled only after Traktor has analyzed/loaded the track.
    traktor_key: str | None = None
    rekordbox_key_original: str | None = None
    traktor_preview_key: str | None = None
    traktor_analysis_key: str | None = None
    active_key_source: ActiveKeySource | None = None
    raw_bpm: float | None = None
    normalized_bpm: float | None = None
    bpm_source: str | None = None
    bpm_was_octave_adjusted: bool = False
    raw_key: str | None = None
    normalized_key: KeyModel | None = None
    key_source: str | None = None
    preferred_display_key: str | None = None
    traktor_musical_key_value: int | None = None
    key_conflict: bool = False
    key_source_confidence: str = "none"

    def __post_init__(self) -> None:
        if self.rekordbox_key_original is None:
            self.rekordbox_key_original = self.rekordbox_key
        if self.raw_bpm is None:
            self.raw_bpm = self.bpm
        if self.normalized_bpm is None:
            self.normalized_bpm, self.bpm_was_octave_adjusted = normalize_bpm(self.raw_bpm)
        if self.normalized_bpm is not None:
            self.bpm = self.normalized_bpm
        if self.bpm_source is None and self.raw_bpm is not None:
            self.bpm_source = "rekordbox"
        if self.raw_key is None:
            self.raw_key = self.rekordbox_key_original
        if self.normalized_key is None:
            self.normalized_key = normalize_key(self.raw_key, self.traktor_musical_key_value)
        self.key_conflict = self.normalized_key.key_conflict
        self.key_source_confidence = self.normalized_key.key_source_confidence
        if self.key_source is None and self.normalized_key.is_valid:
            self.key_source = "rekordbox"
        if self.preferred_display_key is None and self.normalized_key.is_valid:
            self.preferred_display_key = self.normalized_key.open_key
        if self.rekordbox_key is None:
            self.rekordbox_key = self.rekordbox_key_original
        if self.traktor_preview_key is None:
            self.traktor_preview_key = (
                convert_rekordbox_key(self.rekordbox_key_original)
                or self.normalized_key.open_key
            )
        if self.traktor_analysis_key is None:
            self.traktor_analysis_key = self.traktor_key
        if self.traktor_analysis_key:
            self.active_key_source = ActiveKeySource.TRAKTOR_ANALYZED
        elif self.traktor_preview_key:
            self.active_key_source = ActiveKeySource.REKORDBOX_IMPORTED

    @property
    def active_key(self) -> str | None:
        if self.traktor_analysis_key:
            return self.traktor_analysis_key
        return self.traktor_preview_key

    def set_traktor_analysis_key(self, value: str | None) -> None:
        self.traktor_analysis_key = value
        self.traktor_key = value
        self.active_key_source = (
            ActiveKeySource.TRAKTOR_ANALYZED
            if value
            else (ActiveKeySource.REKORDBOX_IMPORTED if self.traktor_preview_key else None)
        )
        self.normalized_key = normalize_key(value)
        if self.normalized_key.is_valid:
            self.key_source = "traktor"
            self.preferred_display_key = self.normalized_key.open_key
        else:
            self.normalized_key = normalize_key(self.raw_key)
            self.key_source = "rekordbox" if self.normalized_key.is_valid else None
            self.preferred_display_key = self.normalized_key.open_key
        self.key_conflict = self.normalized_key.key_conflict
        self.key_source_confidence = self.normalized_key.key_source_confidence

    def set_traktor_analysis_bpm(self, value: float | None) -> None:
        self.bpm_source = "traktor" if value is not None else "rekordbox"
        self.raw_bpm = value if value is not None else self.raw_bpm
        self.normalized_bpm, self.bpm_was_octave_adjusted = normalize_bpm(self.raw_bpm)
        if self.normalized_bpm is not None:
            self.bpm = self.normalized_bpm

    @property
    def key(self) -> str | None:
        """Compatibility alias for code written before schema v2."""
        return self.active_key


@dataclass(slots=True)
class LibraryPlaylist:
    playlist_id: int
    name: str
    parent_id: int | None
    sequence: int
    is_folder: bool
    track_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class LibrarySummary:
    source: str
    database_path: str
    manifest_path: str | None = None
    tracks: int = 0
    playlist_folders: int = 0
    playlists: int = 0
    playlist_entries: int = 0
    tracks_with_bpm: int = 0
    tracks_with_key: int = 0
    tracks_with_beatgrid: int = 0
    tracks_with_hotcues: int = 0
    tracks_with_memory_cues: int = 0
    tracks_with_saved_loops: int = 0
    hot_cues: int = 0
    memory_cues: int = 0
    saved_loops: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class UnifiedLibrary:
    schema_version: int
    generated_at: str
    session_id: str
    usb_root: str
    source: str
    database_path: str
    tracks: list[LibraryTrack] = field(default_factory=list)
    playlists: list[LibraryPlaylist] = field(default_factory=list)
    summary: LibrarySummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    source_id: str
    device_name: str
    connected: bool
    status: str
    playlists_count: int
    tracks_count: int
    last_update: str | None


@dataclass(frozen=True, slots=True)
class PlaylistView:
    id: int
    name: str
    folder: bool
    track_count: int


@dataclass(frozen=True, slots=True)
class TrackView:
    title: str
    artist: str | None
    bpm: float | None
    key: str | None
    source_path: str
    availability: TrackAvailability
    analysis_state: TrackAnalysisState
