from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .source_write_guard import (
    SourceWriteBoundary,
    SourceWriteBoundaryError,
    WriteDenial,
    WriteIntent,
    atomic_write_bytes,
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def atomic_write_json(
    path: Path,
    payload: dict[str, Any],
    *,
    boundary: SourceWriteBoundary | None = None,
) -> None:
    if boundary is None:
        raise SourceWriteBoundaryError(WriteDenial.SOURCE_IDENTITY_UNAVAILABLE)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(boundary, path, encoded, intent=WriteIntent.STATE)


def default_data_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "RekordboxUSB"
