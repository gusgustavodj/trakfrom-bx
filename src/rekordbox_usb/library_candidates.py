from __future__ import annotations

import ntpath
from dataclasses import dataclass
from enum import StrEnum

from .windows_drives import (
    REKORDBOX_LIBRARY_MARKERS,
    PreflightStatus,
    RekordboxPreflightReport,
    RekordboxStructureMarker,
)


class RekordboxLibraryKind(StrEnum):
    """Structural origin of a candidate library marker, not content compatibility."""

    DEVICE_LIBRARY = "device_library"
    DEVICE_LIBRARY_PLUS = "device_library_plus"
    DEVICE_LIBRARY_PLUS_AGENT = "device_library_plus_agent"


class LibrarySelectionState(StrEnum):
    """Whether a later stage must choose between the structural candidates."""

    NONE = "none"
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"
    INELIGIBLE = "ineligible"


@dataclass(frozen=True, slots=True)
class RekordboxLibraryCandidate:
    """One recognized library marker represented without opening its file."""

    volume_root: str
    library_kind: RekordboxLibraryKind
    relative_path: str
    absolute_path: str
    preflight_status: PreflightStatus
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RekordboxLibraryCandidates:
    """All structural library candidates for one preflight report volume."""

    volume_root: str
    preflight_status: PreflightStatus
    candidates: tuple[RekordboxLibraryCandidate, ...]
    selection_state: LibrarySelectionState
    warnings: tuple[str, ...]


_LIBRARY_MARKER_KINDS: tuple[tuple[RekordboxStructureMarker, RekordboxLibraryKind], ...] = (
    (REKORDBOX_LIBRARY_MARKERS[0], RekordboxLibraryKind.DEVICE_LIBRARY),
    (REKORDBOX_LIBRARY_MARKERS[1], RekordboxLibraryKind.DEVICE_LIBRARY_PLUS),
    (REKORDBOX_LIBRARY_MARKERS[2], RekordboxLibraryKind.DEVICE_LIBRARY_PLUS_AGENT),
)
_ELIGIBLE_PREFLIGHT_STATUSES = {
    PreflightStatus.READY,
    PreflightStatus.READY_WITH_WARNINGS,
}


def _windows_path_key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def _selection_state(candidate_count: int, eligible: bool) -> LibrarySelectionState:
    if not eligible:
        return LibrarySelectionState.INELIGIBLE
    if candidate_count == 0:
        return LibrarySelectionState.NONE
    if candidate_count == 1:
        return LibrarySelectionState.UNIQUE
    return LibrarySelectionState.AMBIGUOUS


def build_library_candidates(
    report: RekordboxPreflightReport,
) -> tuple[RekordboxLibraryCandidates, ...]:
    """Build read-only candidates from an A1.4 report without filesystem access.

    Candidate order follows the existing marker order solely for deterministic
    output. It does not select or prefer any candidate.
    """
    results: list[RekordboxLibraryCandidates] = []
    for volume in report.volumes:
        status = PreflightStatus(volume.status)
        eligible = status in _ELIGIBLE_PREFLIGHT_STATUSES
        evidence_paths = {_windows_path_key(item.path) for item in volume.evidence}
        candidates: list[RekordboxLibraryCandidate] = []
        if eligible:
            for marker, library_kind in _LIBRARY_MARKER_KINDS:
                relative_path = str(marker.relative_path)
                absolute_path = ntpath.join(volume.root, relative_path)
                if _windows_path_key(absolute_path) not in evidence_paths:
                    continue
                candidates.append(
                    RekordboxLibraryCandidate(
                        volume_root=volume.root,
                        library_kind=library_kind,
                        relative_path=relative_path,
                        absolute_path=absolute_path,
                        preflight_status=status,
                        warnings=volume.warnings,
                    )
                )
        results.append(
            RekordboxLibraryCandidates(
                volume_root=volume.root,
                preflight_status=status,
                candidates=tuple(candidates),
                selection_state=_selection_state(len(candidates), eligible),
                warnings=volume.warnings,
            )
        )
    return tuple(results)
