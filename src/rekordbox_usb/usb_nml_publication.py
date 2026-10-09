"""Application boundary from one resolved USB session to an NML publication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .library_models import UnifiedLibrary
from .catalog_visual_root import catalog_visual_root
from .models import LibrarySource, LibrarySourceType, UsbIdentity, UsbSession
from .virtual_nml_catalog import (
    NmlCatalog,
    TrackOriginNmlPathResolver,
    VirtualTrackMetadataResolver,
    rebase_nml_catalog_root,
    render_nml_catalog,
)
from .virtual_projection import ProjectionSnapshot
from .volume_access import VolumeIdentity


class UsbNmlPublicationError(ValueError):
    """Base error for an incoherent USB-to-NML publication request."""


class InvalidUsbNmlPublicationIdentityError(UsbNmlPublicationError):
    """Raised when current session evidence does not match the stable source."""


class UsbNmlPublicationContractError(UsbNmlPublicationError):
    """Raised when one pipeline stage returns an incoherent result."""


@dataclass(frozen=True, slots=True)
class UsbNmlPublicationConfig:
    """Stable source identity and revision for one already-resolved session."""

    identity: UsbIdentity
    source: LibrarySource
    snapshot_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, UsbIdentity):
            raise TypeError("identity must be a UsbIdentity")
        if not isinstance(self.source, LibrarySource):
            raise TypeError("source must be a LibrarySource")
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id:
            raise ValueError("snapshot_id must be a non-empty string")


@runtime_checkable
class UsbLibraryReader(Protocol):
    """Read one already-selected USB session through the existing library API."""

    def read(self, session: UsbSession) -> UnifiedLibrary:
        """Return the session's unified library."""


@runtime_checkable
class UsbLibraryProjectionBuilder(Protocol):
    """Adapt one unified library to the existing projection builder contract."""

    def build(
        self,
        library: UnifiedLibrary,
        *,
        volume_identity: VolumeIdentity,
        source_id: str,
        display_name: str,
        snapshot_id: str,
    ) -> ProjectionSnapshot:
        """Return a coherent platform-neutral projection snapshot."""


class NmlCatalogMaterialization(Protocol):
    """Minimum successful materialization result consumed by the service."""

    changed: bool


class NmlCatalogPublicationState(Protocol):
    """Minimum publisher state exposed by a mounted NML catalog adapter."""

    source_id: str
    snapshot_id: str
    mount_point: Path | None
    vhdx_path: Path


class NmlCatalogPublication(Protocol):
    """Minimum publication result implemented by the Windows VHDX adapter."""

    state: NmlCatalogPublicationState
    materialization: NmlCatalogMaterialization | None


@runtime_checkable
class NmlCatalogPublisher(Protocol):
    """Publish an in-memory NML catalog through an externally composed adapter."""

    def publish(self, catalog: NmlCatalog) -> NmlCatalogPublication:
        """Publish the catalog and return mounted publication state."""


@runtime_checkable
class UsbNmlPathResolverFactory(Protocol):
    """Build a current-mount path resolver for one publication request."""

    def build(
        self,
        session: UsbSession,
        config: UsbNmlPublicationConfig,
        volume_identity: VolumeIdentity,
    ) -> TrackOriginNmlPathResolver:
        """Return the resolver bound to this session's current mounted root."""


@runtime_checkable
class UsbNmlPublisherFactory(Protocol):
    """Build one isolated catalog publisher for a stable USB source."""

    def build(
        self,
        identity: UsbIdentity,
        source: LibrarySource,
    ) -> NmlCatalogPublisher:
        """Return a publisher for exactly one source identity."""


@runtime_checkable
class UsbNmlMetadataResolverFactory(Protocol):
    """Build an NML metadata resolver from the library read for this request."""

    def build(self, library: UnifiedLibrary) -> VirtualTrackMetadataResolver | None:
        """Return optional metadata already represented by the unified library."""


@dataclass(frozen=True, slots=True)
class UsbNmlPublicationResult:
    """Structured outcome of the complete in-process composition."""

    source_id: str
    snapshot_id: str
    playlist_count: int
    track_count: int
    catalog: NmlCatalog
    publisher_result: NmlCatalogPublication
    changed: bool
    mount_point: Path | None
    vhdx_path: Path


class UsbNmlPublicationService:
    """Compose existing read, projection, NML, and publication boundaries."""

    def __init__(
        self,
        reader: UsbLibraryReader,
        projection_builder: UsbLibraryProjectionBuilder,
        path_resolver: TrackOriginNmlPathResolver | None = None,
        publisher: NmlCatalogPublisher | None = None,
        *,
        metadata_resolver: VirtualTrackMetadataResolver | None = None,
        path_resolver_factory: UsbNmlPathResolverFactory | None = None,
        publisher_factory: UsbNmlPublisherFactory | None = None,
        metadata_resolver_factory: UsbNmlMetadataResolverFactory | None = None,
    ) -> None:
        if not isinstance(reader, UsbLibraryReader):
            raise TypeError("reader must implement UsbLibraryReader")
        if not isinstance(projection_builder, UsbLibraryProjectionBuilder):
            raise TypeError(
                "projection_builder must implement UsbLibraryProjectionBuilder"
            )
        if path_resolver is None and path_resolver_factory is None:
            raise TypeError("a path_resolver or path_resolver_factory is required")
        if path_resolver is not None and path_resolver_factory is not None:
            raise TypeError("path_resolver and path_resolver_factory are exclusive")
        if path_resolver is not None and not isinstance(
            path_resolver, TrackOriginNmlPathResolver
        ):
            raise TypeError(
                "path_resolver must implement TrackOriginNmlPathResolver"
            )
        if path_resolver_factory is not None and not isinstance(
            path_resolver_factory, UsbNmlPathResolverFactory
        ):
            raise TypeError(
                "path_resolver_factory must implement UsbNmlPathResolverFactory"
            )
        if publisher is None and publisher_factory is None:
            raise TypeError("a publisher or publisher_factory is required")
        if publisher is not None and publisher_factory is not None:
            raise TypeError("publisher and publisher_factory are exclusive")
        if publisher is not None and not isinstance(publisher, NmlCatalogPublisher):
            raise TypeError("publisher must implement NmlCatalogPublisher")
        if publisher_factory is not None and not isinstance(
            publisher_factory, UsbNmlPublisherFactory
        ):
            raise TypeError("publisher_factory must implement UsbNmlPublisherFactory")
        if metadata_resolver is not None and not isinstance(
            metadata_resolver, VirtualTrackMetadataResolver
        ):
            raise TypeError(
                "metadata_resolver must implement VirtualTrackMetadataResolver"
            )
        if metadata_resolver is not None and metadata_resolver_factory is not None:
            raise TypeError(
                "metadata_resolver and metadata_resolver_factory are exclusive"
            )
        if metadata_resolver_factory is not None and not isinstance(
            metadata_resolver_factory, UsbNmlMetadataResolverFactory
        ):
            raise TypeError(
                "metadata_resolver_factory must implement UsbNmlMetadataResolverFactory"
            )
        self._reader = reader
        self._projection_builder = projection_builder
        self._path_resolver = path_resolver
        self._publisher = publisher
        self._metadata_resolver = metadata_resolver
        self._path_resolver_factory = path_resolver_factory
        self._publisher_factory = publisher_factory
        self._metadata_resolver_factory = metadata_resolver_factory

    def publish(
        self,
        session: UsbSession,
        config: UsbNmlPublicationConfig,
    ) -> UsbNmlPublicationResult:
        """Publish one resolved session without discovery, global state, or USB writes."""
        volume_identity = self._validate_identity(session, config)
        library = self._reader.read(session)
        self._validate_library(library, session)
        snapshot = self._projection_builder.build(
            library,
            volume_identity=volume_identity,
            source_id=config.source.source_id,
            display_name=config.source.display_name,
            snapshot_id=config.snapshot_id,
        )
        self._validate_snapshot(snapshot, config, volume_identity)
        path_resolver = self._path_resolver_for(session, config, volume_identity)
        metadata_resolver = self._metadata_resolver_for(library)
        catalog = render_nml_catalog(
            snapshot,
            path_resolver,
            metadata_resolver=metadata_resolver,
        )
        catalog = rebase_nml_catalog_root(
            catalog,
            catalog_visual_root(session.volume_label or config.identity.volume_label, config.identity.identity_id),
        )
        self._validate_catalog(catalog, config)
        publisher_result = self._publisher_for(config).publish(catalog)
        state, changed = self._validate_publication(publisher_result, config)
        return UsbNmlPublicationResult(
            source_id=catalog.source_id,
            snapshot_id=catalog.snapshot_id,
            playlist_count=len(catalog.entries),
            track_count=len(snapshot.tracks),
            catalog=catalog,
            publisher_result=publisher_result,
            changed=changed,
            mount_point=state.mount_point,
            vhdx_path=state.vhdx_path,
        )

    @staticmethod
    def _validate_identity(
        session: UsbSession,
        config: UsbNmlPublicationConfig,
    ) -> VolumeIdentity:
        if not isinstance(session, UsbSession):
            raise TypeError("session must be a UsbSession")
        if not isinstance(config, UsbNmlPublicationConfig):
            raise TypeError("config must be a UsbNmlPublicationConfig")
        if not session.session_id or not session.root:
            raise InvalidUsbNmlPublicationIdentityError(
                "session must have an id and mounted root"
            )
        identity = config.identity
        source = config.source
        if (
            not identity.identity_id
            or not source.source_id
            or not source.display_name
        ):
            raise InvalidUsbNmlPublicationIdentityError(
                "identity and source fields must be non-empty"
            )
        if source.type is not LibrarySourceType.REKORDBOX_USB:
            raise InvalidUsbNmlPublicationIdentityError(
                "source must identify a Rekordbox USB"
            )
        if source.usb_identity_id != identity.identity_id:
            raise InvalidUsbNmlPublicationIdentityError(
                "source does not belong to the configured USB identity"
            )
        if not identity.connected:
            raise InvalidUsbNmlPublicationIdentityError(
                "configured USB identity is disconnected"
            )
        if (
            not session.volume_serial
            or not identity.volume_serial
            or session.volume_serial.casefold() != identity.volume_serial.casefold()
        ):
            raise InvalidUsbNmlPublicationIdentityError(
                "session volume serial does not match the configured USB identity"
            )
        return VolumeIdentity(identity.identity_id)

    def _path_resolver_for(
        self,
        session: UsbSession,
        config: UsbNmlPublicationConfig,
        volume_identity: VolumeIdentity,
    ) -> TrackOriginNmlPathResolver:
        if self._path_resolver_factory is None:
            assert self._path_resolver is not None
            return self._path_resolver
        resolver = self._path_resolver_factory.build(session, config, volume_identity)
        if not isinstance(resolver, TrackOriginNmlPathResolver):
            raise UsbNmlPublicationContractError(
                "path resolver factory returned an invalid resolver"
            )
        return resolver

    def _publisher_for(
        self,
        config: UsbNmlPublicationConfig,
    ) -> NmlCatalogPublisher:
        if self._publisher_factory is None:
            assert self._publisher is not None
            return self._publisher
        publisher = self._publisher_factory.build(config.identity, config.source)
        if not isinstance(publisher, NmlCatalogPublisher):
            raise UsbNmlPublicationContractError(
                "publisher factory returned an invalid publisher"
            )
        return publisher

    def _metadata_resolver_for(
        self,
        library: UnifiedLibrary,
    ) -> VirtualTrackMetadataResolver | None:
        if self._metadata_resolver_factory is None:
            return self._metadata_resolver
        resolver = self._metadata_resolver_factory.build(library)
        if resolver is not None and not isinstance(
            resolver, VirtualTrackMetadataResolver
        ):
            raise UsbNmlPublicationContractError(
                "metadata resolver factory returned an invalid resolver"
            )
        return resolver

    @staticmethod
    def _validate_library(library: UnifiedLibrary, session: UsbSession) -> None:
        if not isinstance(library, UnifiedLibrary):
            raise UsbNmlPublicationContractError(
                "reader must return a UnifiedLibrary"
            )
        if library.session_id != session.session_id:
            raise UsbNmlPublicationContractError(
                "library session_id does not match the requested session"
            )

    @staticmethod
    def _validate_snapshot(
        snapshot: ProjectionSnapshot,
        config: UsbNmlPublicationConfig,
        volume_identity: VolumeIdentity,
    ) -> None:
        if not isinstance(snapshot, ProjectionSnapshot):
            raise UsbNmlPublicationContractError(
                "projection builder must return a ProjectionSnapshot"
            )
        if (
            snapshot.source.source_id != config.source.source_id
            or snapshot.source.volume_identity != volume_identity
            or snapshot.snapshot_id != config.snapshot_id
        ):
            raise UsbNmlPublicationContractError(
                "projection identity does not match the publication request"
            )

    @staticmethod
    def _validate_catalog(
        catalog: NmlCatalog,
        config: UsbNmlPublicationConfig,
    ) -> None:
        if (
            catalog.source_id != config.source.source_id
            or catalog.snapshot_id != config.snapshot_id
        ):
            raise UsbNmlPublicationContractError(
                "catalog identity does not match the publication request"
            )

    @staticmethod
    def _validate_publication(
        result: NmlCatalogPublication,
        config: UsbNmlPublicationConfig,
    ) -> tuple[NmlCatalogPublicationState, bool]:
        try:
            state = result.state
            materialization = result.materialization
            if (
                state.source_id != config.source.source_id
                or state.snapshot_id != config.snapshot_id
            ):
                raise UsbNmlPublicationContractError(
                    "publisher identity does not match the publication request"
                )
            if materialization is None or not isinstance(
                materialization.changed, bool
            ):
                raise UsbNmlPublicationContractError(
                    "publisher must return materialization state"
                )
            if not isinstance(state.vhdx_path, Path):
                raise UsbNmlPublicationContractError(
                    "publisher must return a VHDX path"
                )
            if state.mount_point is not None and not isinstance(
                state.mount_point, Path
            ):
                raise UsbNmlPublicationContractError(
                    "publisher mount point must be a Path or None"
                )
        except AttributeError as exc:
            raise UsbNmlPublicationContractError(
                "publisher returned an invalid result"
            ) from exc
        return state, materialization.changed
