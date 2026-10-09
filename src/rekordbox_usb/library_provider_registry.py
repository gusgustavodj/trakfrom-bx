"""Explicit, immutable registry for neutral library providers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .library_format import LibraryFormatDetection
from .library_provider import (
    LibraryProvider,
    LibraryProviderId,
    LibraryProviderUnavailableError,
)
from .library_selection import SelectedLibrary


class DuplicateLibraryProviderIdError(ValueError):
    """Raised when one registry receives the same provider identity twice."""

    provider_id: LibraryProviderId

    def __init__(self, provider_id: LibraryProviderId) -> None:
        self.provider_id = provider_id
        super().__init__(f"duplicate library provider id: {provider_id.value}")


@dataclass(frozen=True, slots=True)
class LibraryProviderRegistry:
    """An explicit, ordered collection of read-only library providers."""

    providers: tuple[LibraryProvider, ...]

    def __init__(self, providers: Iterable[LibraryProvider] = ()) -> None:
        registered_providers = tuple(providers)
        registered_ids: set[LibraryProviderId] = set()
        for provider in registered_providers:
            if provider.provider_id in registered_ids:
                raise DuplicateLibraryProviderIdError(provider.provider_id)
            registered_ids.add(provider.provider_id)
        object.__setattr__(self, "providers", registered_providers)

    def get(self, provider_id: LibraryProviderId) -> LibraryProvider | None:
        """Return the provider with this identity, or ``None`` when absent."""
        return next(
            (provider for provider in self.providers if provider.provider_id == provider_id),
            None,
        )

    def resolve(
        self,
        selected_library: SelectedLibrary,
        format_detection: LibraryFormatDetection,
    ) -> LibraryProvider:
        """Return the first explicitly registered provider that supports the library."""
        for provider in self.providers:
            if provider.supports(selected_library, format_detection):
                return provider
        raise LibraryProviderUnavailableError()


@runtime_checkable
class LibraryProviderRegistryFactory(Protocol):
    """Build one provider registry bound to a single library extraction."""

    def __call__(
        self,
        *,
        session_id: str,
        usb_root: Path,
        include_grid_points: bool = False,
        include_analysis: bool = True,
    ) -> LibraryProviderRegistry:
        """Return a new registry for the supplied extraction context."""
