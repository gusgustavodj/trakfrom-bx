from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError

import pytest

from rekordbox_usb.library_access import LibraryPrefixRead
from rekordbox_usb.library_candidates import RekordboxLibraryCandidate, RekordboxLibraryKind
from rekordbox_usb.library_format import (
    LibraryFormatDetection,
    LibraryFormatDetectionState,
    RekordboxLibraryFormat,
)
from rekordbox_usb.library_models import UnifiedLibrary
from rekordbox_usb.library_provider import (
    LibraryProviderCapability,
    LibraryProviderId,
    LibraryProviderUnavailableError,
    LibraryProviderUnavailableReason,
)
from rekordbox_usb.library_provider_registry import (
    DuplicateLibraryProviderIdError,
    LibraryProviderRegistry,
)
from rekordbox_usb.library_selection import (
    LibrarySelectionMethod,
    LibrarySelectionReason,
    SelectedLibrary,
)
from rekordbox_usb.windows_drives import PreflightStatus


class _SyntheticProvider:
    capabilities = frozenset({LibraryProviderCapability.READ_LIBRARY})

    def __init__(self, provider_id: str, supports: bool) -> None:
        self.provider_id = LibraryProviderId(provider_id)
        self._supports = supports
        self.calls: list[str] = []

    def supports(
        self,
        selected_library: SelectedLibrary,
        format_detection: LibraryFormatDetection,
    ) -> bool:
        self.calls.append("supports")
        return self._supports

    def read(self, selected_library: SelectedLibrary) -> UnifiedLibrary:
        self.calls.append("read")
        return UnifiedLibrary(
            schema_version=1,
            generated_at="",
            session_id="",
            usb_root="",
            source="synthetic",
            database_path="",
        )


def _selected_library() -> SelectedLibrary:
    candidate = RekordboxLibraryCandidate(
        volume_root="E:\\",
        library_kind=RekordboxLibraryKind.DEVICE_LIBRARY,
        relative_path="PIONEER\\rekordbox\\export.pdb",
        absolute_path="E:\\PIONEER\\rekordbox\\export.pdb",
        preflight_status=PreflightStatus.READY,
        warnings=(),
    )
    return SelectedLibrary(
        candidate=candidate,
        selection_method=LibrarySelectionMethod.EXPLICIT,
        selection_reason=LibrarySelectionReason.ONLY_CANDIDATE,
        available_candidates=(candidate,),
        preflight_status=PreflightStatus.READY,
        warnings=(),
    )


def _format_detection(selected_library: SelectedLibrary) -> LibraryFormatDetection:
    prefix_read = LibraryPrefixRead(
        selected_library=selected_library,
        requested_bytes=1,
        data=b"",
        end_of_file=True,
    )
    return LibraryFormatDetection(
        prefix_read=prefix_read,
        expected_kind=selected_library.candidate.library_kind,
        detected_format=RekordboxLibraryFormat.UNKNOWN,
        state=LibraryFormatDetectionState.EMPTY,
        compatible_with_candidate=False,
        evidence=("EMPTY_FILE",),
        warnings=(),
    )


def test_empty_registry_is_valid() -> None:
    assert LibraryProviderRegistry().providers == ()


def test_registry_preserves_explicit_provider_order() -> None:
    first = _SyntheticProvider("first", supports=False)
    second = _SyntheticProvider("second", supports=True)

    registry = LibraryProviderRegistry((first, second))

    assert registry.providers == (first, second)


def test_registry_copies_mutable_input_into_an_immutable_collection() -> None:
    first = _SyntheticProvider("first", supports=False)
    second = _SyntheticProvider("second", supports=True)
    supplied_providers = [first]

    registry = LibraryProviderRegistry(supplied_providers)
    supplied_providers.append(second)

    assert registry.providers == (first,)
    with pytest.raises(AttributeError):
        registry.providers.append(second)
    with pytest.raises(FrozenInstanceError):
        registry.providers = (second,)


def test_get_returns_the_provider_with_the_requested_identity() -> None:
    provider = _SyntheticProvider("available", supports=True)
    registry = LibraryProviderRegistry((provider,))

    assert registry.get(LibraryProviderId("available")) is provider


def test_get_returns_none_for_an_absent_identity() -> None:
    registry = LibraryProviderRegistry()

    assert registry.get(LibraryProviderId("missing")) is None


def test_duplicate_provider_identity_is_rejected() -> None:
    duplicate_id = LibraryProviderId("duplicate")

    with pytest.raises(DuplicateLibraryProviderIdError) as exc_info:
        LibraryProviderRegistry(
            (
                _SyntheticProvider(duplicate_id.value, supports=False),
                _SyntheticProvider(duplicate_id.value, supports=True),
            )
        )

    assert exc_info.value.provider_id == duplicate_id
    assert str(exc_info.value) == "duplicate library provider id: duplicate"


def test_resolve_returns_the_first_compatible_provider_only() -> None:
    selected_library = _selected_library()
    format_detection = _format_detection(selected_library)
    first = _SyntheticProvider("first", supports=False)
    second = _SyntheticProvider("second", supports=True)
    third = _SyntheticProvider("third", supports=True)

    provider = LibraryProviderRegistry((first, second, third)).resolve(
        selected_library,
        format_detection,
    )

    assert provider is second
    assert first.calls == ["supports"]
    assert second.calls == ["supports"]
    assert third.calls == []


def test_resolve_without_a_compatible_provider_raises_the_stable_error() -> None:
    selected_library = _selected_library()
    format_detection = _format_detection(selected_library)
    provider = _SyntheticProvider("unsupported", supports=False)

    with pytest.raises(LibraryProviderUnavailableError) as exc_info:
        LibraryProviderRegistry((provider,)).resolve(selected_library, format_detection)

    assert exc_info.value.reason is LibraryProviderUnavailableReason.PROVIDER_UNAVAILABLE
    assert provider.calls == ["supports"]


def test_construction_and_resolution_never_call_read() -> None:
    selected_library = _selected_library()
    format_detection = _format_detection(selected_library)
    provider = _SyntheticProvider("supported", supports=True)

    registry = LibraryProviderRegistry((provider,))
    assert provider.calls == []

    assert registry.resolve(selected_library, format_detection) is provider
    assert provider.calls == ["supports"]


def test_import_is_isolated_and_does_not_register_concrete_providers() -> None:
    script = """
import importlib
import json
import sys

before = set(sys.modules)
module = importlib.import_module("rekordbox_usb.library_provider_registry")
blocked = (
    "rekordbox_usb.onelibrary",
    "pyrekordbox",
    "sqlcipher3",
    "sqlcipher3.dbapi2",
)
print(json.dumps({
    "blocked_loaded": [name for name in blocked if name in set(sys.modules) - before],
    "registered_providers": len(module.LibraryProviderRegistry().providers),
}))
"""

    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {"blocked_loaded": [], "registered_providers": 0}
