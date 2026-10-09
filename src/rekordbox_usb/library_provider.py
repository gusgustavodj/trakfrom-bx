"""Neutral contracts for future read-only DJ library providers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .library_format import LibraryFormatDetection
from .library_models import UnifiedLibrary
from .library_selection import SelectedLibrary


class LibraryProviderCapability(StrEnum):
    """Generic operations a library provider can make available."""

    READ_LIBRARY = "read_library"
    TRACKS = "tracks"
    PLAYLISTS = "playlists"
    ANALYSIS = "analysis"


@dataclass(frozen=True, slots=True)
class LibraryProviderId:
    """Stable, non-empty identifier for a library provider."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("provider_id must be a non-empty string")


class LibraryProviderUnavailableReason(StrEnum):
    """Stable reason codes for unavailable library providers."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROTECTED_LIBRARY_REJECTED = "protected_library_rejected"
    PROTECTED_LIBRARY_RUNTIME_UNAVAILABLE = "protected_library_runtime_unavailable"


class LibraryProviderUnavailableError(Exception):
    """Raised when no provider is available for a selected library."""

    reason: LibraryProviderUnavailableReason

    def __init__(
        self,
        reason: LibraryProviderUnavailableReason = LibraryProviderUnavailableReason.PROVIDER_UNAVAILABLE,
    ) -> None:
        self.reason = reason
        super().__init__(reason.value)


@runtime_checkable
class LibraryProvider(Protocol):
    """Read-only provider contract over already selected library contracts."""

    provider_id: LibraryProviderId
    capabilities: frozenset[LibraryProviderCapability]

    def supports(
        self,
        selected_library: SelectedLibrary,
        format_detection: LibraryFormatDetection,
    ) -> bool:
        """Return whether this provider can read the confirmed library format."""

    def read(self, selected_library: SelectedLibrary) -> UnifiedLibrary:
        """Read one selected library into the neutral library model."""
