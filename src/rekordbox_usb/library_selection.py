from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .library_candidates import (
    LibrarySelectionState,
    RekordboxLibraryCandidate,
    RekordboxLibraryCandidates,
    RekordboxLibraryKind,
)
from .windows_drives import PreflightStatus


class LibrarySelectionMethod(StrEnum):
    """How a candidate was selected for a later read-only library stage."""

    EXPLICIT = "explicit"
    AUTOMATIC = "automatic"


class LibrarySelectionReason(StrEnum):
    """Stable reason codes for library selection outcomes."""

    USER_SELECTED = "user_selected"
    ONLY_CANDIDATE = "only_candidate"
    ONLY_SUPPORTED_CANDIDATE = "only_supported_candidate"
    FORMAT_PREFERENCE = "format_preference"
    AMBIGUOUS_SELECTION_REQUIRED = "ambiguous_selection_required"
    PREFERRED_FORMAT_INVALID = "preferred_format_invalid"
    NO_CANDIDATE = "no_candidate"
    INELIGIBLE = "ineligible"
    NOT_FOUND = "not_found"


class LibrarySelectionError(Exception):
    """Base error for a selection that cannot be completed safely."""

    reason: LibrarySelectionReason

    def __init__(self, reason: LibrarySelectionReason):
        self.reason = reason
        super().__init__(reason.value)


class SelectionRequiredError(LibrarySelectionError):
    def __init__(self):
        super().__init__(LibrarySelectionReason.AMBIGUOUS_SELECTION_REQUIRED)


class CandidateNotFoundError(LibrarySelectionError):
    def __init__(self):
        super().__init__(LibrarySelectionReason.NOT_FOUND)


class IneligibleLibraryError(LibrarySelectionError):
    def __init__(self):
        super().__init__(LibrarySelectionReason.INELIGIBLE)


class NoLibraryCandidateError(LibrarySelectionError):
    def __init__(self):
        super().__init__(LibrarySelectionReason.NO_CANDIDATE)


class PreferredFormatInvalidError(LibrarySelectionError):
    """Raised when an executable preferred format fails structural detection."""

    def __init__(self):
        super().__init__(LibrarySelectionReason.PREFERRED_FORMAT_INVALID)


@dataclass(frozen=True, slots=True)
class SelectedLibrary:
    """An explicit, traceable selection of one structural library candidate."""

    candidate: RekordboxLibraryCandidate
    selection_method: LibrarySelectionMethod
    selection_reason: LibrarySelectionReason
    available_candidates: tuple[RekordboxLibraryCandidate, ...]
    preflight_status: PreflightStatus
    warnings: tuple[str, ...]


def select_library(
    candidates: RekordboxLibraryCandidates,
    library_kind: RekordboxLibraryKind | None = None,
) -> SelectedLibrary:
    """Select a candidate only when an explicit request makes it unambiguous."""
    if candidates.selection_state is LibrarySelectionState.INELIGIBLE:
        raise IneligibleLibraryError()
    if candidates.selection_state is LibrarySelectionState.NONE:
        raise NoLibraryCandidateError()
    if candidates.selection_state is LibrarySelectionState.AMBIGUOUS and library_kind is None:
        raise SelectionRequiredError()

    matching = tuple(
        candidate
        for candidate in candidates.candidates
        if library_kind is None or candidate.library_kind is library_kind
    )
    if not matching:
        raise CandidateNotFoundError()
    if len(matching) != 1:
        raise SelectionRequiredError()

    reason = (
        LibrarySelectionReason.ONLY_CANDIDATE
        if library_kind is None
        else LibrarySelectionReason.USER_SELECTED
    )
    return SelectedLibrary(
        candidate=matching[0],
        selection_method=LibrarySelectionMethod.EXPLICIT,
        selection_reason=reason,
        available_candidates=candidates.candidates,
        preflight_status=candidates.preflight_status,
        warnings=candidates.warnings,
    )
