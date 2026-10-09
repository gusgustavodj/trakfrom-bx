from __future__ import annotations

import re


class KeyConverter:
    _CAMELOT_PATTERN = re.compile(r"^(1[0-2]|[1-9])([AB])$", re.IGNORECASE)

    @classmethod
    def convert_rekordbox_key(cls, value: str | None) -> str | None:
        if not value:
            return None
        match = cls._CAMELOT_PATTERN.fullmatch(value.strip())
        if not match:
            return None
        number = int(match.group(1))
        mode = "m" if match.group(2).upper() == "A" else "d"
        traktor_number = ((number + 4) % 12) + 1
        return f"{traktor_number}{mode}"


def convert_rekordbox_key(value: str | None) -> str | None:
    return KeyConverter.convert_rekordbox_key(value)
