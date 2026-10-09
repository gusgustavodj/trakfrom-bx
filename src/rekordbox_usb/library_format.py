from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .library_access import LibraryPrefixRead
from .library_candidates import RekordboxLibraryKind


SQLITE_HEADER_SIGNATURE = b"SQLite format 3\x00"
LEGACY_PDB_PAGE_SIZE = 4096
LEGACY_PDB_DIRECTORY_COUNT = 20
LEGACY_PDB_DIRECTORY_OFFSET = 28
LEGACY_PDB_DIRECTORY_ENTRY_SIZE = 16
LEGACY_PDB_MINIMUM_PREFIX_BYTES = (
    LEGACY_PDB_DIRECTORY_OFFSET
    + LEGACY_PDB_DIRECTORY_COUNT * LEGACY_PDB_DIRECTORY_ENTRY_SIZE
)
_LEGACY_REQUIRED_FAMILIES = {0, 7, 8}


class RekordboxLibraryFormat(StrEnum):
    """File envelope formats confirmed from the already-read prefix only."""

    SQLITE = "sqlite"
    LEGACY_PDB = "legacy_pdb"
    UNKNOWN = "unknown"


class LibraryFormatDetectionState(StrEnum):
    """Certainty and compatibility state of a bounded format inspection."""

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    EMPTY = "empty"
    TRUNCATED = "truncated"
    UNKNOWN = "unknown"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class LibraryFormatDetection:
    """Pure format identification based solely on a ``LibraryPrefixRead``."""

    prefix_read: LibraryPrefixRead
    expected_kind: RekordboxLibraryKind
    detected_format: RekordboxLibraryFormat
    state: LibraryFormatDetectionState
    compatible_with_candidate: bool | None
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]


def _is_truncated_sqlite_signature(data: bytes) -> bool:
    return data != b"" and SQLITE_HEADER_SIGNATURE.startswith(data)


def _detection(
    prefix_read: LibraryPrefixRead,
    *,
    detected_format: RekordboxLibraryFormat,
    state: LibraryFormatDetectionState,
    compatible_with_candidate: bool | None,
    evidence: tuple[str, ...],
    warnings: tuple[str, ...] = (),
) -> LibraryFormatDetection:
    selected_library = prefix_read.selected_library
    return LibraryFormatDetection(
        prefix_read=prefix_read,
        expected_kind=selected_library.candidate.library_kind,
        detected_format=detected_format,
        state=state,
        compatible_with_candidate=compatible_with_candidate,
        evidence=evidence,
        warnings=(*selected_library.warnings, *warnings),
    )


def _expected_kind_evidence(kind: RekordboxLibraryKind) -> str:
    return f"EXPECTED_{kind.name}"


def _device_library_detection(prefix_read: LibraryPrefixRead) -> LibraryFormatDetection:
    data = prefix_read.data
    expected = _expected_kind_evidence(RekordboxLibraryKind.DEVICE_LIBRARY)
    if len(data) < LEGACY_PDB_MINIMUM_PREFIX_BYTES:
        if prefix_read.end_of_file:
            return _detection(
                prefix_read,
                detected_format=RekordboxLibraryFormat.UNKNOWN,
                state=LibraryFormatDetectionState.TRUNCATED,
                compatible_with_candidate=False,
                evidence=("LEGACY_HEADER_TRUNCATED", expected),
                warnings=("LEGACY_HEADER_TRUNCATED",),
            )
        return _detection(
            prefix_read,
            detected_format=RekordboxLibraryFormat.UNKNOWN,
            state=LibraryFormatDetectionState.UNCONFIRMED,
            compatible_with_candidate=None,
            evidence=("PREFIX_INSUFFICIENT", expected),
            warnings=("PREFIX_INSUFFICIENT",),
        )
    if prefix_read.end_of_file:
        return _detection(
            prefix_read,
            detected_format=RekordboxLibraryFormat.UNKNOWN,
            state=LibraryFormatDetectionState.TRUNCATED,
            compatible_with_candidate=False,
            evidence=("LEGACY_FILE_SMALLER_THAN_ONE_PAGE", expected),
            warnings=("LEGACY_FILE_SMALLER_THAN_ONE_PAGE",),
        )

    reserved_zero = data[0:4] == b"\x00" * 4 and data[24:28] == b"\x00" * 4
    page_size = int.from_bytes(data[4:8], "little")
    directory_count = int.from_bytes(data[8:12], "little")
    if page_size != LEGACY_PDB_PAGE_SIZE or directory_count != LEGACY_PDB_DIRECTORY_COUNT:
        return _detection(
            prefix_read,
            detected_format=RekordboxLibraryFormat.UNKNOWN,
            state=LibraryFormatDetectionState.MISMATCH,
            compatible_with_candidate=False,
            evidence=("LEGACY_V1_ENVELOPE_UNSUPPORTED", expected),
            warnings=("LEGACY_V1_ENVELOPE_UNSUPPORTED",),
        )
    family_codes = [
        int.from_bytes(
            data[
                LEGACY_PDB_DIRECTORY_OFFSET + index * LEGACY_PDB_DIRECTORY_ENTRY_SIZE :
                LEGACY_PDB_DIRECTORY_OFFSET + index * LEGACY_PDB_DIRECTORY_ENTRY_SIZE + 4
            ],
            "little",
        )
        for index in range(LEGACY_PDB_DIRECTORY_COUNT)
    ]
    if not reserved_zero or any(family_codes.count(code) != 1 for code in _LEGACY_REQUIRED_FAMILIES):
        return _detection(
            prefix_read,
            detected_format=RekordboxLibraryFormat.UNKNOWN,
            state=LibraryFormatDetectionState.MISMATCH,
            compatible_with_candidate=False,
            evidence=("LEGACY_V1_ENVELOPE_INVALID", expected),
            warnings=("LEGACY_V1_ENVELOPE_INVALID",),
        )
    return _detection(
        prefix_read,
        detected_format=RekordboxLibraryFormat.LEGACY_PDB,
        state=LibraryFormatDetectionState.CONFIRMED,
        compatible_with_candidate=True,
        evidence=("LEGACY_V1_ENVELOPE_CONFIRMED", expected),
    )


def detect_library_format(prefix_read: LibraryPrefixRead) -> LibraryFormatDetection:
    """Identify only a supported file envelope without opening or reading files.

    Device Library confirmation uses only the approved neutral V1 envelope.
    """
    if not isinstance(prefix_read, LibraryPrefixRead):
        raise TypeError("prefix_read must be a LibraryPrefixRead")

    data = prefix_read.data
    expected_kind = prefix_read.selected_library.candidate.library_kind
    if data == b"":
        if prefix_read.end_of_file:
            return _detection(
                prefix_read,
                detected_format=RekordboxLibraryFormat.UNKNOWN,
                state=LibraryFormatDetectionState.EMPTY,
                compatible_with_candidate=False,
                evidence=("EMPTY_FILE",),
            )
        return _detection(
            prefix_read,
            detected_format=RekordboxLibraryFormat.UNKNOWN,
            state=LibraryFormatDetectionState.UNCONFIRMED,
            compatible_with_candidate=None,
            evidence=("PREFIX_INSUFFICIENT",),
            warnings=("PREFIX_INSUFFICIENT",),
        )

    if data.startswith(SQLITE_HEADER_SIGNATURE):
        if expected_kind is RekordboxLibraryKind.DEVICE_LIBRARY:
            return _detection(
                prefix_read,
                detected_format=RekordboxLibraryFormat.SQLITE,
                state=LibraryFormatDetectionState.MISMATCH,
                compatible_with_candidate=False,
                evidence=(
                    "SQLITE_HEADER_CONFIRMED",
                    _expected_kind_evidence(expected_kind),
                    "FORMAT_MISMATCH",
                ),
                warnings=("FORMAT_MISMATCH",),
            )
        return _detection(
            prefix_read,
            detected_format=RekordboxLibraryFormat.SQLITE,
            state=LibraryFormatDetectionState.CONFIRMED,
            compatible_with_candidate=True,
            evidence=("SQLITE_HEADER_CONFIRMED", _expected_kind_evidence(expected_kind)),
        )

    if _is_truncated_sqlite_signature(data):
        if prefix_read.end_of_file:
            return _detection(
                prefix_read,
                detected_format=RekordboxLibraryFormat.UNKNOWN,
                state=LibraryFormatDetectionState.TRUNCATED,
                compatible_with_candidate=False,
                evidence=("SQLITE_HEADER_TRUNCATED",),
                warnings=("SQLITE_HEADER_TRUNCATED",),
            )
        return _detection(
            prefix_read,
            detected_format=RekordboxLibraryFormat.UNKNOWN,
            state=LibraryFormatDetectionState.UNCONFIRMED,
            compatible_with_candidate=None,
            evidence=("PREFIX_INSUFFICIENT",),
            warnings=("PREFIX_INSUFFICIENT",),
        )

    if expected_kind is RekordboxLibraryKind.DEVICE_LIBRARY:
        return _device_library_detection(prefix_read)

    return _detection(
        prefix_read,
        detected_format=RekordboxLibraryFormat.UNKNOWN,
        state=LibraryFormatDetectionState.MISMATCH,
        compatible_with_candidate=False,
        evidence=("UNKNOWN_HEADER", _expected_kind_evidence(expected_kind), "FORMAT_MISMATCH"),
        warnings=("FORMAT_MISMATCH",),
    )
