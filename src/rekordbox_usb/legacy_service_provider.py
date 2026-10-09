"""Bind the confirmed Legacy reader to the service's explicit provider contract."""

from __future__ import annotations

import ntpath
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .legacy_compatibility import CleanCompatibilityProvider, CompatibilityFailure
from .legacy_reader import read_legacy
from .library_candidates import RekordboxLibraryKind
from .library_format import (
    LibraryFormatDetection, LibraryFormatDetectionState, RekordboxLibraryFormat,
)
from .library_models import (
    LibraryPlaylist, LibraryTrack, TrackAnalysis, TrackAnalysisState,
    TrackAvailability, UnifiedLibrary,
)
from .library_provider import LibraryProviderCapability, LibraryProviderId
from .library_selection import SelectedLibrary


class _ServiceFactory:
    @staticmethod
    def validate_id(value: int) -> bool:
        return 0 < value <= 2**63 - 1

    @staticmethod
    def track(**fields) -> LibraryTrack:
        fields.pop("availability_fact")
        analysis = fields.pop("analysis")
        fields["analysis"] = TrackAnalysis(
            data_path=analysis["data_path"],
            resolved_dat_path=analysis["resolved_dat_path"],
            beat_count=analysis["beat_count"],
            bpm_min=analysis["bpm_min"],
            bpm_max=analysis["bpm_max"],
            first_beat_seconds=analysis["first_beat_seconds"],
            last_beat_seconds=analysis["last_beat_seconds"],
            variable_bpm=analysis["variable_bpm"],
            hot_cues=analysis["cues"],
            saved_loops=analysis["loops"],
            beat_grid=analysis["grid"],
            warnings=analysis["warnings"],
        )
        fields["analysis_state"] = TrackAnalysisState.UNKNOWN
        fields["availability"] = TrackAvailability.MISSING_USB
        # Reuse the existing normalization for optional source metadata.
        fields.pop("normalized_key")
        return LibraryTrack(**fields)

    @staticmethod
    def playlist(**fields) -> LibraryPlaylist:
        return LibraryPlaylist(**fields)

    @staticmethod
    def library(**fields) -> UnifiedLibrary:
        return UnifiedLibrary(**fields)


class LegacyServiceProvider:
    """One session's provider; selection is explicit and never falls back."""

    provider_id = LibraryProviderId(CleanCompatibilityProvider.provider_id)
    capabilities = frozenset({
        LibraryProviderCapability.READ_LIBRARY,
        LibraryProviderCapability.TRACKS,
        LibraryProviderCapability.PLAYLISTS,
    })

    def __init__(
        self, session_id: str, usb_root: Path,
        reader: Callable = read_legacy,
    ) -> None:
        self.session_id = session_id
        self.usb_root = usb_root
        self._selection: SelectedLibrary | None = None
        self._detection: dict | None = None
        root = str(usb_root).replace("\\", "/")
        context = {
            "session_id": session_id,
            "usb_root": root,
            "candidate_path": root.rstrip("/") + "/PIONEER/rekordbox/export.pdb",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._clean = CleanCompatibilityProvider(reader, _ServiceFactory(), context)

    def supports(
        self, selected_library: SelectedLibrary,
        format_detection: LibraryFormatDetection,
    ) -> bool:
        self._selection = None
        self._detection = None
        if not isinstance(selected_library, SelectedLibrary) or not isinstance(format_detection, LibraryFormatDetection):
            return False
        candidate = selected_library.candidate
        if candidate.library_kind is not RekordboxLibraryKind.DEVICE_LIBRARY:
            return False
        if candidate.relative_path.replace("\\", "/") != "PIONEER/rekordbox/export.pdb":
            return False
        root = str(self.usb_root).replace("\\", "/")
        candidate_path = candidate.absolute_path.replace("\\", "/")
        if ntpath.normcase(candidate.volume_root) != ntpath.normcase(str(self.usb_root)):
            return False
        if candidate_path != root.rstrip("/") + "/PIONEER/rekordbox/export.pdb":
            return False
        detection = {
            "format": "legacy_v1_confirmed" if format_detection.detected_format is RekordboxLibraryFormat.LEGACY_PDB and format_detection.state is LibraryFormatDetectionState.CONFIRMED and format_detection.compatible_with_candidate is True else "unknown",
            "unambiguous": True,
            "read_library": True,
        }
        if not self._clean.supports("legacy_confirmed", detection):
            return False
        self._selection = selected_library
        self._detection = detection
        return True

    def read(self, selected_library: SelectedLibrary) -> UnifiedLibrary:
        if selected_library != self._selection or self._detection is None:
            raise CompatibilityFailure("UNSUPPORTED")
        return self._clean.read("legacy_confirmed", self._detection)
