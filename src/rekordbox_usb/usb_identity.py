from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .models import LibraryKind, LibrarySource, LibrarySourceType, UsbIdentity, UsbSession
from .util import atomic_write_json, utc_now_iso
from .source_write_guard import (
    LocalDestinationBinding, SourceWriteBoundary, SourceWriteBoundaryError, WriteDenial,
)


class UsbIdentityStore:
    def __init__(self, path: Path):
        self.path = path
        self._local_bindings: dict[str, LocalDestinationBinding] = {}

    @staticmethod
    def _library_paths(session: UsbSession) -> tuple[str | None, str | None]:
        export_library_path = next(
            (item.path for item in session.libraries if item.kind is LibraryKind.ONE_LIBRARY), None
        )
        device_library_path = next(
            (item.path for item in session.libraries if item.kind is LibraryKind.DEVICE_LIBRARY),
            None,
        )
        return export_library_path, device_library_path

    @staticmethod
    def _hash_library(path: str | None) -> str:
        if not path:
            return ""
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            while chunk := handle.read(4 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _identity_id(volume_serial: str | None) -> str:
        if not volume_serial:
            raise ValueError("Rekordbox USB identity requires a Windows volume serial")
        # The library hash is observed metadata, not identity material: Rekordbox
        # legitimately changes exportLibrary.db as the user updates the USB.
        seed = volume_serial.upper().encode("utf-8")
        return hashlib.blake2s(seed, digest_size=16).hexdigest()

    def _load(self) -> tuple[dict[str, UsbIdentity], dict[str, LibrarySource]]:
        if not self.path.exists():
            return {}, {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        identities = {
            item["identity_id"]: UsbIdentity(**item) for item in payload.get("identities", [])
        }
        sources = {
            item["source_id"]: LibrarySource(
                source_id=item["source_id"],
                type=LibrarySourceType(item["type"]),
                display_name=item["display_name"],
                usb_identity_id=item["usb_identity_id"],
            )
            for item in payload.get("sources", [])
        }
        return identities, sources

    def _write(
        self,
        identities: dict[str, UsbIdentity],
        sources: dict[str, LibrarySource],
        *,
        source_root: Path,
    ) -> None:
        atomic_write_json(
            self.path,
            {
                "schema_version": 1,
                "identities": [asdict(item) for item in identities.values()],
                "sources": [asdict(item) for item in sources.values()],
            },
            boundary=SourceWriteBoundary(source_root),
        )

    def observe(self, session: UsbSession) -> tuple[UsbIdentity, LibrarySource]:
        export_library_path, device_library_path = self._library_paths(session)
        library_hash = self._hash_library(export_library_path or device_library_path)
        identity_id = self._identity_id(session.volume_serial)
        identities, sources = self._load()
        now = utc_now_iso()
        existing = identities.get(identity_id)
        identity = UsbIdentity(
            identity_id=identity_id,
            volume_serial=session.volume_serial,
            volume_label=session.volume_label,
            mount_point=session.root,
            export_library_path=export_library_path,
            device_library_path=device_library_path,
            library_hash=library_hash,
            first_seen=existing.first_seen if existing else now,
            last_seen=now,
            connected=True,
        )
        source_id = f"rekordbox-usb:{identity_id}"
        source = sources.get(source_id) or LibrarySource(
            source_id=source_id,
            type=LibrarySourceType.REKORDBOX_USB,
            display_name=session.volume_label or f"rekordbox USB {identity_id[:8]}",
            usb_identity_id=identity_id,
        )
        identities[identity_id] = identity
        sources[source_id] = source
        self._write(identities, sources, source_root=Path(session.root))
        self._local_bindings[identity_id] = SourceWriteBoundary(
            Path(session.root)
        ).capture_local_destination(self.path)
        return identity, source

    def mark_disconnected(self, identity_id: str) -> UsbIdentity:
        identities, sources = self._load()
        identity = identities.get(identity_id)
        if identity is None:
            raise KeyError(f"Unknown USB identity: {identity_id}")
        identity.connected = False
        identity.last_seen = utc_now_iso()
        binding = self._local_bindings.get(identity_id)
        if binding is None:
            raise SourceWriteBoundaryError(WriteDenial.SOURCE_IDENTITY_UNAVAILABLE)
        atomic_write_json(
            self.path,
            {
                "schema_version": 1,
                "identities": [asdict(item) for item in identities.values()],
                "sources": [asdict(item) for item in sources.values()],
            },
            boundary=SourceWriteBoundary.from_local_binding(binding),
        )
        return identity
