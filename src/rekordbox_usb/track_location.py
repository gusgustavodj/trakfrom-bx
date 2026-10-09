from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from .library_models import LibraryTrack, TrackAvailability


class TrackLocationMode(StrEnum):
    USB_DIRECT = "usb_direct"
    CACHE_LEGACY = "cache_legacy"


class TrackLocationError(FileNotFoundError):
    """Raised when a track has no readable location for the selected mode."""


class TrackLocationResolver:
    def __init__(
        self,
        mode: TrackLocationMode = TrackLocationMode.USB_DIRECT,
        *,
        legacy_locations: Mapping[int, str] | None = None,
    ) -> None:
        self.mode = TrackLocationMode(mode)
        self._legacy_locations = dict(legacy_locations or {})

    def resolve(self, track: LibraryTrack) -> str:
        if self.mode is TrackLocationMode.USB_DIRECT:
            location = track.source_path
        else:
            location = self._legacy_locations.get(track.track_id, "")
            if not location:
                raise TrackLocationError(
                    f"Legacy cache location not provided for track {track.track_id}: {track.title}"
                )

        path = Path(location)
        if not path.is_file():
            if self.mode is TrackLocationMode.USB_DIRECT:
                track.availability = TrackAvailability.MISSING_USB
            raise TrackLocationError(
                f"Track file not found for {self.mode.value} mode "
                f"(track {track.track_id}: {track.title}): {path}"
            )
        track.availability = (
            TrackAvailability.AVAILABLE_USB
            if self.mode is TrackLocationMode.USB_DIRECT
            else TrackAvailability.CACHE_LEGACY_AVAILABLE
        )
        return str(path)
