from __future__ import annotations

from dataclasses import dataclass
import inspect
from pathlib import Path

import pytest

from rekordbox_usb.library_models import LibraryPlaylist, LibraryTrack, UnifiedLibrary
from rekordbox_usb.models import (
    LibrarySource,
    LibrarySourceType,
    UsbIdentity,
    UsbSession,
)
from rekordbox_usb.usb_nml_publication import (
    InvalidUsbNmlPublicationIdentityError,
    NmlCatalogPublisher,
    UsbNmlPublicationConfig,
    UsbNmlPublicationService,
)
from rekordbox_usb.virtual_projection_builder import (
    ProjectionBuildInput,
    ProjectionPlaylistInput,
    ProjectionTrackInput,
    build_projection_snapshot,
)
from rekordbox_usb.volume_access import (
    TrackOrigin,
    VirtualVolumePath,
    VolumeIdentity,
)
from rekordbox_usb.windows_vhdx_nml_adapter import WindowsVhdxNmlVolumeAdapter


@dataclass
class _Materialization:
    changed: bool


@dataclass
class _PublicationState:
    source_id: str
    snapshot_id: str
    mount_point: Path | None
    vhdx_path: Path


@dataclass
class _Publication:
    state: _PublicationState
    materialization: _Materialization


class _Reader:
    def __init__(self, library: UnifiedLibrary) -> None:
        self.library = library
        self.calls = 0
        self.error: Exception | None = None

    def read(self, session: UsbSession) -> UnifiedLibrary:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.library


class _Projector:
    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None
        self.volume_identities: list[VolumeIdentity] = []

    def build(
        self,
        library: UnifiedLibrary,
        *,
        volume_identity: VolumeIdentity,
        source_id: str,
        display_name: str,
        snapshot_id: str,
    ):
        self.calls += 1
        self.volume_identities.append(volume_identity)
        if self.error is not None:
            raise self.error
        tracks_by_id = {track.track_id: track for track in library.tracks}
        playlists = tuple(
            ProjectionPlaylistInput(
                str(playlist.playlist_id),
                source_id,
                playlist.name,
                playlist.is_folder,
                0 if playlist.is_folder else len(playlist.track_ids),
                str(playlist.parent_id) if playlist.parent_id is not None else None,
            )
            for playlist in library.playlists
        )
        tracks = tuple(
            ProjectionTrackInput(
                str(track_id),
                source_id,
                str(playlist.playlist_id),
                TrackOrigin(
                    "synthetic",
                    volume_identity,
                    VirtualVolumePath(tracks_by_id[track_id].source_path),
                    str(track_id),
                ),
                tracks_by_id[track_id].title,
                tracks_by_id[track_id].file_size,
            )
            for playlist in library.playlists
            if not playlist.is_folder
            for track_id in playlist.track_ids
        )
        return build_projection_snapshot(
            ProjectionBuildInput(
                volume_identity,
                source_id,
                display_name,
                snapshot_id,
                playlists,
                tracks,
            )
        )


class _Resolver:
    def resolve(self, origin: TrackOrigin) -> str:
        return rf"X:\{origin.virtual_path.value}"


class _Publisher:
    def __init__(self, *, changed: bool = True) -> None:
        self.changed = changed
        self.calls = 0
        self.catalogs = []
        self.error: Exception | None = None

    def publish(self, catalog):
        self.calls += 1
        self.catalogs.append(catalog)
        if self.error is not None:
            raise self.error
        return _Publication(
            _PublicationState(
                catalog.source_id,
                catalog.snapshot_id,
                Path("C:/mounted"),
                Path("C:/managed/catalog.vhdx"),
            ),
            _Materialization(self.changed),
        )


class _TypedPublisherError(ValueError):
    pass


def _track(track_id: int, title: str) -> LibraryTrack:
    return LibraryTrack(
        track_id,
        title,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        f"Music/{title}.mp3",
        f"Music/{title}.mp3",
        f"{title}.mp3",
        100,
    )


def _library(
    session_id: str = "session-current",
    *,
    empty_second: bool = False,
) -> UnifiedLibrary:
    tracks = [_track(1, "One")]
    playlists = [
        LibraryPlaylist(10, "First", None, 0, False, [1]),
        LibraryPlaylist(
            20,
            "Second",
            None,
            1,
            False,
            [] if empty_second else [1],
        ),
    ]
    return UnifiedLibrary(
        1,
        "2026-07-27T00:00:00Z",
        session_id,
        "D:\\",
        "one_library",
        "D:\\PIONEER\\rekordbox\\exportLibrary.db",
        tracks,
        playlists,
    )


def _session(root: str = "D:\\", session_id: str = "session-current") -> UsbSession:
    return UsbSession(
        session_id=session_id,
        root=root,
        volume_label="USB",
        volume_serial="F0731C72",
    )


def _config(mount: str = "D:\\") -> UsbNmlPublicationConfig:
    identity = UsbIdentity(
        "stable-volume-id",
        "F0731C72",
        "USB",
        mount,
        None,
        None,
        "",
        "before",
        "now",
        True,
    )
    source = LibrarySource(
        "rekordbox-usb:stable-volume-id",
        LibrarySourceType.REKORDBOX_USB,
        "USB",
        identity.identity_id,
    )
    return UsbNmlPublicationConfig(identity, source, "snapshot-1")


def _service(
    library: UnifiedLibrary | None = None,
    *,
    changed: bool = True,
) -> tuple[UsbNmlPublicationService, _Reader, _Projector, _Publisher]:
    reader = _Reader(library or _library())
    projector = _Projector()
    publisher = _Publisher(changed=changed)
    return (
        UsbNmlPublicationService(reader, projector, _Resolver(), publisher),
        reader,
        projector,
        publisher,
    )


def test_valid_session_composes_each_stage_once_and_returns_two_nmls() -> None:
    service, reader, projector, publisher = _service()

    result = service.publish(_session(), _config())

    assert (reader.calls, projector.calls, publisher.calls) == (1, 1, 1)
    assert result.source_id == "rekordbox-usb:stable-volume-id"
    assert result.snapshot_id == "snapshot-1"
    assert result.playlist_count == 2
    assert result.track_count == 2
    assert len(result.catalog.entries) == 2
    assert result.publisher_result is not None
    assert result.mount_point == Path("C:/mounted")
    assert result.vhdx_path == Path("C:/managed/catalog.vhdx")
    assert all(entry.destination_path.segments[0] == "USB" for entry in result.catalog.entries)


def test_session_label_changes_only_the_materialized_catalog_root() -> None:
    service, _, projector, publisher = _service()
    session = _session()
    session.volume_label = "GUS CULT 25"

    result = service.publish(session, _config())

    assert result.source_id == "rekordbox-usb:stable-volume-id"
    assert result.snapshot_id == "snapshot-1"
    assert projector.volume_identities == [VolumeIdentity("stable-volume-id")]
    assert all(entry.destination_path.segments[0] == "GUS CULT 25" for entry in result.catalog.entries)
    assert all("GUS CULT 25" not in entry.content for entry in publisher.catalogs[0].entries)


def test_mount_letter_change_preserves_source_and_volume_identity() -> None:
    first, _, first_projector, _ = _service()
    second_library = _library(session_id="session-remounted")
    second, _, second_projector, _ = _service(second_library)

    first_result = first.publish(_session("D:\\"), _config("D:\\"))
    second_result = second.publish(
        _session("E:\\", "session-remounted"),
        _config("E:\\"),
    )

    assert first_result.source_id == second_result.source_id
    assert first_projector.volume_identities == second_projector.volume_identities
    assert first_projector.volume_identities == [VolumeIdentity("stable-volume-id")]


def test_identity_mismatch_stops_before_reader() -> None:
    service, reader, projector, publisher = _service()
    session = _session()
    session.volume_serial = "OTHER"

    with pytest.raises(InvalidUsbNmlPublicationIdentityError):
        service.publish(session, _config())

    assert reader.calls == projector.calls == publisher.calls == 0


def test_empty_playlist_follows_current_catalog_contract() -> None:
    service, _, _, _ = _service(_library(empty_second=True))

    result = service.publish(_session(), _config())

    assert len(result.catalog.entries) == 2
    assert result.catalog.entries[1].track_count == 0
    assert 'ENTRIES="0"' in result.catalog.entries[1].content


def test_reader_failure_stops_projection_and_publication() -> None:
    service, reader, projector, publisher = _service()
    reader.error = OSError("reader failed")

    with pytest.raises(OSError, match="reader failed"):
        service.publish(_session(), _config())

    assert projector.calls == publisher.calls == 0


def test_projection_failure_stops_publication() -> None:
    service, _, projector, publisher = _service()
    projector.error = ValueError("projection failed")

    with pytest.raises(ValueError, match="projection failed"):
        service.publish(_session(), _config())

    assert publisher.calls == 0


def test_typed_publisher_error_is_preserved() -> None:
    service, _, _, publisher = _service()
    publisher.error = _TypedPublisherError("publication failed")

    with pytest.raises(_TypedPublisherError, match="publication failed"):
        service.publish(_session(), _config())


@pytest.mark.parametrize("changed", [False, True])
def test_changed_is_propagated_from_publisher(changed: bool) -> None:
    service, _, _, _ = _service(changed=changed)

    assert service.publish(_session(), _config()).changed is changed


def test_vhdx_adapter_structurally_satisfies_publisher_port() -> None:
    assert isinstance(
        object.__new__(WindowsVhdxNmlVolumeAdapter),
        NmlCatalogPublisher,
    )


def test_service_has_no_global_runtime_platform_or_usb_write_dependencies() -> None:
    import rekordbox_usb.usb_nml_publication as publication

    source = inspect.getsource(publication)
    forbidden = (
        "from .app import",
        "from .runtime_clean import",
        "build_clean_runtime_bridge",
        "PowerShell",
        "DiskPart",
        "WindowsVhdxNmlVolumeAdapter",
        "collection.nml",
        "open(",
        "write_",
        "D:\\\\",
        "E:\\\\",
    )
    assert all(token not in source for token in forbidden)
