"""Safe presentation-only roots for materialized NML catalogs."""

from __future__ import annotations


_INVALID_WINDOWS_COMPONENT_CHARACTERS = frozenset('<>:"/\\|?*')
_RESERVED_WINDOWS_COMPONENTS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
MAX_CATALOG_VISUAL_ROOT_LENGTH = 48


def catalog_visual_root(volume_label: str | None, identity_id: str) -> str:
    """Return one Windows-safe catalog root without changing source identity."""
    fallback = _fallback(identity_id)
    if not isinstance(volume_label, str) or not volume_label.strip():
        return fallback
    candidate = "".join(
        character
        for character in volume_label
        if character not in _INVALID_WINDOWS_COMPONENT_CHARACTERS
        and ord(character) >= 32
    ).rstrip(". ")
    if not candidate or not candidate.strip():
        return fallback
    candidate = candidate[:MAX_CATALOG_VISUAL_ROOT_LENGTH].rstrip(". ")
    if not candidate or _is_reserved(candidate):
        return fallback
    return candidate


def _fallback(identity_id: str) -> str:
    if not isinstance(identity_id, str) or not identity_id:
        raise ValueError("identity_id must be a non-empty string")
    return f"Rekordbox USB - {identity_id[:8].upper()}"


def _is_reserved(component: str) -> bool:
    return component.split(".", maxsplit=1)[0].upper() in _RESERVED_WINDOWS_COMPONENTS
