"""Clean, in-memory boundary for an explicitly selected Legacy V1 library."""

from __future__ import annotations

import re
import math
from typing import Any, Callable, Mapping, Protocol


STATUSES = frozenset({"SUCCESS", "UNSUPPORTED", "INVALID_LIBRARY", "READ_ERROR", "PROVIDER_UNAVAILABLE"})
CAPABILITIES = frozenset({"read_library", "tracks", "playlists"})
PROVIDER_ID = "clean_legacy_v1"
_ID = re.compile(r"[1-9][0-9]*\Z")


class ServiceFactory(Protocol):
    def validate_id(self, value: int) -> bool: ...
    def track(self, **fields: Any) -> Any: ...
    def playlist(self, **fields: Any) -> Any: ...
    def library(self, **fields: Any) -> Any: ...


class CompatibilityFailure(Exception):
    """Sanitized failure. No untrusted exception text is retained."""

    def __init__(self, status: str):
        self.status = status if status in STATUSES and status != "SUCCESS" else "INVALID_LIBRARY"
        super().__init__(self.status)


def _id(value: Any, factory: ServiceFactory) -> int:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CompatibilityFailure("INVALID_LIBRARY")
    number = int(value)
    if factory.validate_id(number) is not True:
        raise CompatibilityFailure("INVALID_LIBRARY")
    return number


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise CompatibilityFailure("INVALID_LIBRARY")
    return value


def _path(value: Any) -> str:
    value = _text(value)
    if (any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or
            value.startswith("/") or "\\" in value or ":" in value or
            any(part in ("", ".", "..") for part in value.split("/"))):
        raise CompatibilityFailure("INVALID_LIBRARY")
    return value


def _object(value: Any, keys: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(key not in value for key in keys):
        raise CompatibilityFailure("INVALID_LIBRARY")
    return value


def _sequence(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise CompatibilityFailure("INVALID_LIBRARY")
    return value


def _selection_ok(context: Mapping[str, Any], candidate: str, detection: Mapping[str, Any]) -> bool:
    if candidate != "legacy_confirmed" or not isinstance(context, Mapping) or not isinstance(detection, Mapping):
        return False
    if detection.get("format") != "legacy_v1_confirmed" or detection.get("unambiguous") is not True:
        return False
    if detection.get("read_library") is not True:
        return False
    root, path, session, timestamp = (context.get(k) for k in
                                      ("usb_root", "candidate_path", "session_id", "generated_at"))
    if not all(isinstance(x, str) and x for x in (root, path, session, timestamp)):
        return False
    root = root.replace("\\", "/")
    path = path.replace("\\", "/")
    if not (root.startswith("/") or re.fullmatch(r"[A-Za-z]:/.*", root)):
        return False
    if "//" in root or any(p in (".", "..") for p in root.split("/")):
        return False
    if path != root.rstrip("/") + "/PIONEER/rekordbox/export.pdb":
        return False
    return True


def _validated_model(raw: Any, factory: ServiceFactory, context: Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    raw = _object(raw, ("tracks", "nodes", "playlists"))
    tracks, nodes, playlists = (_sequence(raw[k]) for k in ("tracks", "nodes", "playlists"))
    track_fields: list[dict] = []
    track_ids: set[int] = set()
    last_id = 0
    for item in tracks:
        item = _object(item, ("id", "title", "relative_audio_path"))
        ident = _id(item["id"], factory)
        if ident <= last_id:
            raise CompatibilityFailure("INVALID_LIBRARY")
        last_id = ident
        track_ids.add(ident)
        path = _path(item["relative_audio_path"])
        bpm = item.get("bpm")
        if bpm is not None and (
            type(bpm) not in (int, float) or not math.isfinite(bpm) or bpm <= 0
        ):
            raise CompatibilityFailure("INVALID_LIBRARY")
        key = item.get("rekordbox_key")
        if key is not None:
            key = _text(key)
        artist = item.get("artist")
        if artist is not None:
            artist = _text(artist)
        track_fields.append({
            "track_id": ident, "title": _text(item["title"]), "source_path": path,
            "rekordbox_path": path, "file_name": path.split("/")[-1],
            "artist": artist, "album": None, "genre": None, "label": None, "bpm": bpm,
            "rekordbox_key": key, "duration_seconds": None, "track_number": None,
            "rating": None, "comment": None, "file_size": None,
            "analysis": {"data_path": None, "resolved_dat_path": None, "beat_count": 0,
                         "bpm_min": None, "bpm_max": None, "first_beat_seconds": None,
                         "last_beat_seconds": None, "variable_bpm": False, "cues": [],
                         "loops": [], "grid": [], "warnings": []},
            "analysis_state": "UNKNOWN", "availability": "MISSING_USB",
            "availability_fact": "UNVERIFIED",
            "traktor_key": None, "traktor_preview_key": None, "traktor_analysis_key": None,
            "active_key_source": None, "traktor_musical_key_value": None,
            "rekordbox_key_original": key, "raw_key": key, "preferred_display_key": None,
            "key_source": None, "raw_bpm": bpm, "normalized_bpm": bpm,
            "bpm_source": "rekordbox" if bpm is not None else None,
            "normalized_key": None, "key_conflict": False, "key_source_confidence": "none",
            "bpm_was_octave_adjusted": False})
    node_fields: list[dict] = []
    node_kinds: dict[int, str] = {}
    child_counts: dict[int | None, int] = {}
    ancestry: list[int] = []
    for item in nodes:
        item = _object(item, ("id", "name", "parent_id", "sequence", "kind"))
        ident = _id(item["id"], factory)
        if ident in node_kinds or item["kind"] not in ("folder", "playlist"):
            raise CompatibilityFailure("INVALID_LIBRARY")
        parent = None if item["parent_id"] is None else _id(item["parent_id"], factory)
        if parent is not None and (node_kinds.get(parent) != "folder" or parent not in ancestry):
            raise CompatibilityFailure("INVALID_LIBRARY")
        if parent is None:
            ancestry.clear()
        else:
            ancestry = ancestry[:ancestry.index(parent) + 1]
        seq = item["sequence"]
        if type(seq) is not int or seq != child_counts.get(parent, 0):
            raise CompatibilityFailure("INVALID_LIBRARY")
        child_counts[parent] = seq + 1
        node_kinds[ident] = item["kind"]
        if item["kind"] == "folder":
            ancestry.append(ident)
        node_fields.append({"playlist_id": ident, "name": _text(item["name"]),
                            "parent_id": parent, "sequence": seq,
                            "is_folder": item["kind"] == "folder", "track_ids": []})
    associations: dict[int, list[int]] = {}
    for item in playlists:
        item = _object(item, ("node_id", "track_ids"))
        ident = _id(item["node_id"], factory)
        if node_kinds.get(ident) != "playlist" or ident in associations:
            raise CompatibilityFailure("INVALID_LIBRARY")
        ids = [_id(v, factory) for v in _sequence(item["track_ids"])]
        if any(v not in track_ids for v in ids):
            raise CompatibilityFailure("INVALID_LIBRARY")
        associations[ident] = ids
    if set(associations) != {ident for ident, kind in node_kinds.items() if kind == "playlist"}:
        raise CompatibilityFailure("INVALID_LIBRARY")
    for item in node_fields:
        if not item["is_folder"]:
            item["track_ids"] = associations[item["playlist_id"]]
    return track_fields, node_fields


class CleanCompatibilityProvider:
    """One operation's explicit reader and model-construction boundary."""

    provider_id = PROVIDER_ID
    capabilities = CAPABILITIES

    def __init__(self, reader: Callable[[str], Mapping[str, Any]], factory: ServiceFactory,
                 context: Mapping[str, Any]):
        self._reader = reader
        self._factory = factory
        self._context = dict(context)

    def supports(self, candidate: str, detection: Mapping[str, Any]) -> bool:
        return _selection_ok(self._context, candidate, detection)

    def read(self, candidate: str, detection: Mapping[str, Any]) -> Any:
        if not self.supports(candidate, detection):
            raise CompatibilityFailure("UNSUPPORTED")
        if not callable(self._reader):
            raise CompatibilityFailure("PROVIDER_UNAVAILABLE")
        try:
            result = self._reader(self._context["usb_root"])
        except Exception:
            raise CompatibilityFailure("READ_ERROR") from None
        if not isinstance(result, Mapping):
            raise CompatibilityFailure("INVALID_LIBRARY")
        status = result.get("status")
        if status not in STATUSES:
            raise CompatibilityFailure("INVALID_LIBRARY")
        if status != "SUCCESS":
            raise CompatibilityFailure(status)
        try:
            tracks, playlists = _validated_model(result.get("library"), self._factory, self._context)
            made_tracks = [self._factory.track(**item) for item in tracks]
            made_playlists = [self._factory.playlist(**item) for item in playlists]
            return self._factory.library(
                schema_version=1, generated_at=self._context["generated_at"],
                session_id=self._context["session_id"], usb_root=self._context["usb_root"],
                source=self.provider_id, database_path=self._context["candidate_path"],
                summary=None, tracks=made_tracks, playlists=made_playlists)
        except Exception:
            raise CompatibilityFailure("INVALID_LIBRARY") from None
