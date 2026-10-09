from __future__ import annotations

import ntpath
from dataclasses import replace
from pathlib import Path

from .library_access import LibraryAccessError, read_library_prefix
from .library_candidates import (
    RekordboxLibraryCandidates,
    RekordboxLibraryKind,
    build_library_candidates,
)
from .library_format import (
    LEGACY_PDB_MINIMUM_PREFIX_BYTES,
    SQLITE_HEADER_SIGNATURE,
    LibraryFormatDetection,
    LibraryFormatDetectionState,
    RekordboxLibraryFormat,
    detect_library_format,
)
from .library_models import LibrarySummary, UnifiedLibrary
from .library_provider import (
    LibraryProvider,
    LibraryProviderUnavailableError,
    LibraryProviderUnavailableReason,
)
from .library_provider_registry import LibraryProviderRegistry
from .library_selection import (
    LibrarySelectionError,
    LibrarySelectionMethod,
    LibrarySelectionReason,
    SelectedLibrary,
    PreferredFormatInvalidError,
    SelectionRequiredError,
    select_library,
)
from .models import LibraryKind, UsbSession
from .track_location import TrackLocationError, TrackLocationResolver
from .util import atomic_write_json
from .source_write_guard import SourceWriteBoundary
from .windows_drives import DriveInfo, build_rekordbox_preflight_report, preflight_rekordbox_volumes


class LibraryExtractionError(RuntimeError):
    """A library extraction failure with safe operational diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        source_path: Path | None = None,
        working_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.source_path = source_path
        self.working_path = working_path

    def diagnostic(self) -> dict[str, str | None]:
        """Return stable, non-sensitive fields suitable for CLI reporting."""
        cause = self.__cause__
        return {
            "error": type(self).__name__,
            "message": str(self),
            "stage": self.stage,
            "cause_type": type(cause).__name__ if cause is not None else None,
            "cause_message": str(cause) if cause is not None else None,
            "source_path": str(self.source_path) if self.source_path is not None else None,
            "working_path": str(self.working_path) if self.working_path is not None else None,
        }


_ONE_LIBRARY_KINDS = frozenset(
    {
        RekordboxLibraryKind.DEVICE_LIBRARY_PLUS,
        RekordboxLibraryKind.DEVICE_LIBRARY_PLUS_AGENT,
    }
)


def _windows_path_key(path: str) -> str:
    """Compare Windows paths lexically without resolving or accessing them."""
    return ntpath.normcase(ntpath.normpath(path))


def select_onelibrary_preferred_library(
    candidates: RekordboxLibraryCandidates,
) -> SelectedLibrary:
    """Select the sole OneLibrary marker before lower-priority Device Library.

    This applies only to the OneLibrary extraction pipeline.  It deliberately
    preserves all candidates on ``SelectedLibrary.available_candidates`` so a
    caller can record that a Device Library marker was ignored.  Multiple
    OneLibrary markers remain ambiguous, and a malformed selected OneLibrary
    never falls back to ``export.pdb`` later in the pipeline.
    """
    one_library_candidates = tuple(
        item for item in candidates.candidates if item.library_kind in _ONE_LIBRARY_KINDS
    )
    if len(one_library_candidates) == 1:
        return select_library(candidates, one_library_candidates[0].library_kind)
    return select_library(candidates)


def _selected_library_from_session(session: UsbSession) -> SelectedLibrary:
    """Rebuild one structural selection and require agreement with discovery."""
    volume = DriveInfo(
        root=Path(session.root),
        label=session.volume_label,
        serial=session.volume_serial,
        filesystem=session.filesystem,
    )
    report = build_rekordbox_preflight_report(preflight_rekordbox_volumes((volume,)))
    candidate_groups = build_library_candidates(report)
    try:
        selected_library = select_onelibrary_preferred_library(candidate_groups[0])
    except LibrarySelectionError as exc:
        source_path = (
            Path(session.libraries[0].path)
            if len(session.libraries) == 1
            else None
        )
        raise LibraryExtractionError(
            "No unambiguous supported Rekordbox library found",
            stage="select_library",
            source_path=source_path,
        ) from exc

    selected_model_kind = (
        LibraryKind.DEVICE_LIBRARY
        if selected_library.candidate.library_kind is RekordboxLibraryKind.DEVICE_LIBRARY
        else LibraryKind.ONE_LIBRARY
    )
    discovered_libraries = tuple(
        item for item in session.libraries if item.kind is selected_model_kind
    )
    if len(discovered_libraries) != 1:
        label = "Device Library" if selected_model_kind is LibraryKind.DEVICE_LIBRARY else "OneLibrary"
        raise LibraryExtractionError(f"Exactly one discovered {label} entry is required")

    legacy_path = discovered_libraries[0].path
    selected_path = selected_library.candidate.absolute_path
    if _windows_path_key(legacy_path) != _windows_path_key(selected_path):
        raise LibraryExtractionError(
            "Legacy library path does not match the selected Rekordbox library"
        )
    return selected_library


def _selected_candidate(
    candidates: RekordboxLibraryCandidates,
    candidate_index: int,
    selection_reason: LibrarySelectionReason,
) -> SelectedLibrary:
    candidate = candidates.candidates[candidate_index]
    return SelectedLibrary(
        candidate=candidate,
        selection_method=LibrarySelectionMethod.AUTOMATIC,
        selection_reason=selection_reason,
        available_candidates=candidates.candidates,
        preflight_status=candidates.preflight_status,
        warnings=candidates.warnings,
    )


def _expected_format_detection(
    detection: LibraryFormatDetection,
) -> LibraryFormatDetection:
    """Normalize one candidate to its expected valid envelope for registry lookup."""
    expected_format = (
        RekordboxLibraryFormat.LEGACY_PDB
        if detection.expected_kind is RekordboxLibraryKind.DEVICE_LIBRARY
        else RekordboxLibraryFormat.SQLITE
    )
    return replace(
        detection,
        detected_format=expected_format,
        state=LibraryFormatDetectionState.CONFIRMED,
        compatible_with_candidate=True,
        evidence=("EXPECTED_FORMAT_FOR_PROVIDER_LOOKUP",),
        warnings=(),
    )


def _provider_aware_selection(
    session: UsbSession,
    provider_registry: LibraryProviderRegistry,
) -> tuple[SelectedLibrary, LibraryProvider]:
    """Select by format and registered support, then apply format preference."""

    volume = DriveInfo(
        root=Path(session.root),
        label=session.volume_label,
        serial=session.volume_serial,
        filesystem=session.filesystem,
    )
    report = build_rekordbox_preflight_report(preflight_rekordbox_volumes((volume,)))
    candidates = build_library_candidates(report)[0]
    if not candidates.candidates:
        select_library(candidates)
    for model_kind in (LibraryKind.DEVICE_LIBRARY, LibraryKind.ONE_LIBRARY):
        structural = tuple(
            candidate
            for candidate in candidates.candidates
            if (
                LibraryKind.DEVICE_LIBRARY
                if candidate.library_kind is RekordboxLibraryKind.DEVICE_LIBRARY
                else LibraryKind.ONE_LIBRARY
            )
            is model_kind
        )
        if not structural:
            continue
        discovered = tuple(item for item in session.libraries if item.kind is model_kind)
        label = "Device Library" if model_kind is LibraryKind.DEVICE_LIBRARY else "OneLibrary"
        if len(structural) == 1 and len(discovered) != 1:
            raise LibraryExtractionError(f"Exactly one discovered {label} entry is required")
        if len(structural) != len(discovered):
            raise LibraryExtractionError(
                f"Discovered {label} entries do not match the library candidates"
            )
        structural_paths = {_windows_path_key(item.absolute_path) for item in structural}
        discovered_paths = {_windows_path_key(item.path) for item in discovered}
        if structural_paths != discovered_paths:
            raise LibraryExtractionError(
                "Discovered library path does not match the selected Rekordbox library"
            )

    for candidate in candidates.candidates:
        model_kind = (
            LibraryKind.DEVICE_LIBRARY
            if candidate.library_kind is RekordboxLibraryKind.DEVICE_LIBRARY
            else LibraryKind.ONE_LIBRARY
        )
        if not any(
            item.kind is model_kind
            and _windows_path_key(item.path) == _windows_path_key(candidate.absolute_path)
            for item in session.libraries
        ):
            raise LibraryExtractionError(
                "Discovered library path does not match the selected Rekordbox library"
            )
    supported: list[tuple[int, SelectedLibrary, LibraryProvider]] = []
    invalid_preferred: list[SelectedLibrary] = []
    first_access_error: LibraryAccessError | None = None
    first_provider_error: LibraryProviderUnavailableError | None = None
    candidate_indexes = sorted(
        range(len(candidates.candidates)),
        key=lambda index: (
            candidates.candidates[index].library_kind not in _ONE_LIBRARY_KINDS,
            candidates.candidates[index].library_kind.value,
        ),
    )
    for index in candidate_indexes:
        initial_reason = (
            LibrarySelectionReason.ONLY_CANDIDATE
            if len(candidates.candidates) == 1
            else LibrarySelectionReason.ONLY_SUPPORTED_CANDIDATE
        )
        selected = _selected_candidate(candidates, index, initial_reason)
        prefix_size = (
            LEGACY_PDB_MINIMUM_PREFIX_BYTES
            if selected.candidate.library_kind is RekordboxLibraryKind.DEVICE_LIBRARY
            else len(SQLITE_HEADER_SIGNATURE)
        )
        try:
            detection = detect_library_format(read_library_prefix(selected, prefix_size))
        except LibraryAccessError as exc:
            if first_access_error is None:
                first_access_error = exc
            continue
        try:
            provider = provider_registry.resolve(selected, detection)
        except LibraryProviderUnavailableError as exc:
            if exc.reason is not LibraryProviderUnavailableReason.PROVIDER_UNAVAILABLE:
                raise
            if (
                detection.state is not LibraryFormatDetectionState.CONFIRMED
                or detection.compatible_with_candidate is not True
            ):
                try:
                    provider_registry.resolve(
                        selected, _expected_format_detection(detection)
                    )
                except LibraryProviderUnavailableError as normalized_exc:
                    if first_provider_error is None:
                        first_provider_error = normalized_exc
                    continue
                if selected.candidate.library_kind in _ONE_LIBRARY_KINDS:
                    invalid_preferred.append(selected)
                continue
            if first_provider_error is None:
                first_provider_error = exc
            continue
        supported.append((index, selected, provider))

    if invalid_preferred:
        raise PreferredFormatInvalidError()

    one_library = tuple(
        item for item in supported if item[1].candidate.library_kind in _ONE_LIBRARY_KINDS
    )
    if len(one_library) > 1:
        raise SelectionRequiredError()
    if len(one_library) == 1:
        selected_index, selected, provider = one_library[0]
        if len(supported) > 1:
            selected = _selected_candidate(
                candidates, selected_index, LibrarySelectionReason.FORMAT_PREFERENCE
            )
    else:
        legacy = tuple(
            item
            for item in supported
            if item[1].candidate.library_kind is RekordboxLibraryKind.DEVICE_LIBRARY
        )
        if len(legacy) > 1:
            raise SelectionRequiredError()
        if len(legacy) != 1:
            if first_access_error is not None:
                raise first_access_error
            if first_provider_error is not None:
                raise first_provider_error
            raise LibraryProviderUnavailableError()
        _, selected, provider = legacy[0]

    return selected, provider


def refresh_track_availability(library: UnifiedLibrary) -> None:
    """Validate already-resolved paths once, without scanning media or reading ANLZ."""
    resolver = TrackLocationResolver()
    for track in getattr(library, "tracks", ()):
        try:
            resolver.resolve(track)
        except TrackLocationError:
            pass


def summarize(library: UnifiedLibrary) -> LibrarySummary:
    tracks = library.tracks
    folders = sum(1 for item in library.playlists if item.is_folder)
    playlists = sum(1 for item in library.playlists if not item.is_folder)
    summary = LibrarySummary(
        source=library.source,
        database_path=library.database_path,
        tracks=len(tracks),
        playlist_folders=folders,
        playlists=playlists,
        playlist_entries=sum(len(item.track_ids) for item in library.playlists),
        tracks_with_bpm=sum(track.bpm is not None for track in tracks),
        tracks_with_key=sum(bool(track.rekordbox_key) for track in tracks),
        tracks_with_beatgrid=sum(track.analysis.beat_count > 0 for track in tracks),
        tracks_with_hotcues=sum(bool(track.analysis.hot_cues) for track in tracks),
        tracks_with_memory_cues=sum(bool(track.analysis.memory_cues) for track in tracks),
        tracks_with_saved_loops=sum(bool(track.analysis.saved_loops) for track in tracks),
        hot_cues=sum(len(track.analysis.hot_cues) for track in tracks),
        memory_cues=sum(len(track.analysis.memory_cues) for track in tracks),
        saved_loops=sum(len(track.analysis.saved_loops) for track in tracks),
    )
    for track in tracks:
        summary.warnings.extend(track.analysis.warnings)
    return summary


def extract_library(
    session: UsbSession,
    data_root: Path,
    include_grid_points: bool = False,
    include_analysis: bool = True,
    *,
    provider_registry: LibraryProviderRegistry,
    persist_manifest: bool = True,
) -> tuple[UnifiedLibrary, Path]:
    if not session.libraries:
        raise LibraryExtractionError("No supported Rekordbox library found")

    try:
        selected_library, provider = _provider_aware_selection(session, provider_registry)
    except LibraryAccessError as exc:
        source_path = Path(session.libraries[0].path) if len(session.libraries) == 1 else None
        raise LibraryExtractionError(
            "Rekordbox library could not be prepared safely",
            stage="read_library_prefix",
            source_path=source_path,
        ) from exc
    except (LibrarySelectionError, LibraryProviderUnavailableError) as exc:
        raise LibraryExtractionError(
            "Rekordbox library could not be prepared safely",
            stage="resolve_library_provider",
        ) from exc
    source_path = Path(selected_library.candidate.absolute_path)

    try:
        library = provider.read(selected_library)
    except Exception as exc:
        raise LibraryExtractionError(
            "Rekordbox library could not be prepared safely",
            stage="read_library_provider",
            source_path=source_path,
        ) from exc
    summary = summarize(library)
    output = data_root / "library" / session.session_id / "library.json"
    summary.manifest_path = str(output) if persist_manifest else None
    library.summary = summary
    if persist_manifest:
        try:
            atomic_write_json(
                output, library.to_dict(), boundary=SourceWriteBoundary(Path(session.root))
            )
        except OSError:
            # Metadata caching is progressive and optional; USB listing must still
            # work when the local data root is unavailable or read-only.
            pass
    return library, output
