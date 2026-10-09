from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True, slots=True)
class KeyModel:
    raw_key: str | None
    canonical_name: str | None
    short_name: str | None
    camelot: str | None
    open_key: str | None
    mode: str | None
    semitone: int | None
    is_valid: bool
    harmonic_id: str | None = None
    wheel_position: int | None = None
    pitch_class: int | None = None
    canonical_flat_name: str | None = None
    canonical_sharp_name: str | None = None
    short_flat: str | None = None
    short_sharp: str | None = None
    traktor_musical_key_value: int | None = None
    key_conflict: bool = False
    key_source_confidence: str = "none"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_NOTES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_CAMELOT_MINOR = {1: "G#", 2: "D#", 3: "A#", 4: "F", 5: "C", 6: "G", 7: "D", 8: "A", 9: "E", 10: "B", 11: "F#", 12: "C#"}
_CAMELOT_MAJOR = {1: "B", 2: "F#", 3: "C#", 4: "G#", 5: "D#", 6: "A#", 7: "F", 8: "C", 9: "G", 10: "D", 11: "A", 12: "E"}
_MINOR_SEMITONES = {"G#": 8, "D#": 3, "A#": 10, "F": 5, "C": 0, "G": 7, "D": 2, "A": 9, "E": 4, "B": 11, "F#": 6, "C#": 1}
_MAJOR_SEMITONES = _MINOR_SEMITONES
_FLAT_MINOR = {1: "A-flat", 2: "E-flat", 3: "B-flat", 4: "F", 5: "C", 6: "G", 7: "D", 8: "A", 9: "E", 10: "B", 11: "F-sharp", 12: "D-flat"}
_FLAT_MAJOR = {1: "B", 2: "F-sharp", 3: "D-flat", 4: "A-flat", 5: "E-flat", 6: "B-flat", 7: "F", 8: "C", 9: "G", 10: "D", 11: "A", 12: "E"}
_SHARP_MINOR = {1: "G-sharp", 2: "D-sharp", 3: "A-sharp", 4: "F", 5: "C", 6: "G", 7: "D", 8: "A", 9: "E", 10: "B", 11: "F-sharp", 12: "C-sharp"}
_SHARP_MAJOR = {1: "B", 2: "F-sharp", 3: "C-sharp", 4: "G-sharp", 5: "D-sharp", 6: "A-sharp", 7: "F", 8: "C", 9: "G", 10: "D", 11: "A", 12: "E"}


def _invalid(raw: str | None) -> KeyModel:
    return KeyModel(raw, None, None, None, None, None, None, False)


def _model(raw, number, mode, note, open_number):
    open_key = f"{open_number}{'m' if mode == 'minor' else 'd'}"
    camelot = f"{number}{'A' if mode == 'minor' else 'B'}"
    flat_name = (_FLAT_MINOR if mode == "minor" else _FLAT_MAJOR)[number]
    sharp_name = (_SHARP_MINOR if mode == "minor" else _SHARP_MAJOR)[number]
    flat = flat_name.replace("-flat", "b").replace("-sharp", "#")
    sharp = sharp_name.replace("-flat", "b").replace("-sharp", "#")
    semitone = _MINOR_SEMITONES.get(note, _MINOR_SEMITONES.get(sharp))
    short_flat = f"{flat}{'m' if mode == 'minor' else ''}"
    short_sharp = f"{sharp}{'m' if mode == 'minor' else ''}"
    canonical_flat = f"{flat_name} {'minor' if mode == 'minor' else 'major'}"
    canonical_sharp = f"{sharp_name} {'minor' if mode == 'minor' else 'major'}"
    return KeyModel(raw, canonical_sharp, short_sharp, camelot, open_key, mode, semitone, True,
                    harmonic_id=open_key, wheel_position=number, pitch_class=semitone,
                    canonical_flat_name=canonical_flat, canonical_sharp_name=canonical_sharp,
                    short_flat=short_flat, short_sharp=short_sharp)


def _normalize_text_key(value: str | None) -> KeyModel:
    if value is None or not str(value).strip():
        return _invalid(value)
    raw = str(value).strip()
    camelot = re.fullmatch(r"(1[0-2]|[1-9])([AB])", raw, re.IGNORECASE)
    if camelot:
        number = int(camelot.group(1))
        mode = "minor" if camelot.group(2).upper() == "A" else "major"
        open_number = ((number + 4) % 12) + 1
        note = (_CAMELOT_MINOR if mode == "minor" else _CAMELOT_MAJOR)[number]
        return _model(raw, number, mode, note, open_number)

    open_match = re.fullmatch(r"(1[0-2]|[1-9])([mMdD])", raw)
    if open_match:
        open_number = int(open_match.group(1))
        mode = "minor" if open_match.group(2).lower() == "m" else "major"
        camelot_number = ((open_number + 6) % 12) + 1
        camelot = f"{camelot_number}{'A' if mode == 'minor' else 'B'}"
        note = (_CAMELOT_MINOR if mode == "minor" else _CAMELOT_MAJOR)[camelot_number]
        return _model(raw, camelot_number, mode, note, open_number)

    musical = re.fullmatch(r"([A-Ga-g])([#b♯♭]?)\s*(m|min|minor|maj|major)?", raw)
    if musical:
        note = musical.group(1).upper() + {"♯": "#", "♭": "b"}.get(musical.group(2), musical.group(2))
        aliases = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
        note = aliases.get(note, note)
        if note not in _NOTES:
            return _invalid(value)
        mode = "minor" if musical.group(3) in {"m", "min", "minor"} else "major"
        matches = _CAMELOT_MINOR if mode == "minor" else _CAMELOT_MAJOR
        camelot_number = next((n for n, candidate in matches.items() if candidate == note), None)
        if camelot_number is None:
            return _invalid(value)
        camelot = f"{camelot_number}{'A' if mode == 'minor' else 'B'}"
        open_number = ((camelot_number + 4) % 12) + 1
        return _model(raw, camelot_number, mode, note, open_number)
    return _invalid(value)


def _from_traktor_value(value: int) -> KeyModel:
    if not 0 <= value <= 23:
        return _invalid(str(value))
    mode = "major" if value < 12 else "minor"
    semitone = value % 12
    notes = {0: "C", 1: "C#", 2: "D", 3: "D#", 4: "E", 5: "F", 6: "F#", 7: "G", 8: "G#", 9: "A", 10: "A#", 11: "B"}
    note = notes[semitone]
    mapping = _CAMELOT_MAJOR if mode == "major" else _CAMELOT_MINOR
    number = next(number for number, candidate in mapping.items() if candidate == note)
    open_number = ((number + 4) % 12) + 1
    return _model(str(value), number, mode, note, open_number)


def normalize_key(value: str | None, traktor_musical_key_value: int | None = None) -> KeyModel:
    """Normalize textual key and optionally confirm it with Traktor's NML value."""
    textual = _normalize_text_key(value)
    if traktor_musical_key_value is None:
        return textual
    musical = _from_traktor_value(traktor_musical_key_value)
    if not musical.is_valid:
        return replace(textual, traktor_musical_key_value=traktor_musical_key_value,
                       key_source_confidence="low" if textual.is_valid else "none")
    if not textual.is_valid:
        return replace(musical, raw_key=value, traktor_musical_key_value=traktor_musical_key_value,
                       key_source_confidence="high")
    converges = textual.open_key == musical.open_key
    return replace(textual, traktor_musical_key_value=traktor_musical_key_value,
                   key_conflict=not converges,
                   key_source_confidence="high" if converges else "low")


def normalize_bpm(value: float | int | None, minimum: float = 79.0, maximum: float = 150.0) -> tuple[float | None, bool]:
    if value is None:
        return None, False
    bpm = float(value)
    if bpm <= 0:
        return None, False
    if bpm < minimum:
        candidate = bpm * 2
        return (candidate, True) if candidate <= maximum else (bpm, False)
    if bpm > maximum:
        candidate = bpm / 2
        return (candidate, True) if candidate >= minimum else (bpm, False)
    return bpm, False
