from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Protocol

from .library_selection import SelectedLibrary


MAX_LIBRARY_PROBE_BYTES = 64 * 1024


class LibraryBinarySource(Protocol):
    """Minimal read-only binary source used for an already selected library."""

    def open_readonly(self, path: str) -> BinaryIO: ...


class _StandardLibraryBinarySource:
    def open_readonly(self, path: str) -> BinaryIO:
        return open(path, "rb")


class LibraryAccessError(Exception):
    """Base error for a bounded read of the selected library."""


class LibraryNotFoundError(LibraryAccessError):
    def __init__(self):
        super().__init__("Selected library file was not found")


class LibraryPermissionError(LibraryAccessError):
    def __init__(self):
        super().__init__("Selected library could not be read")


class LibraryIsDirectoryError(LibraryAccessError):
    def __init__(self):
        super().__init__("Selected library path is a directory")


class LibraryReadError(LibraryAccessError):
    def __init__(self):
        super().__init__("Selected library could not be read")


class InvalidReadLimitError(LibraryAccessError):
    def __init__(self):
        super().__init__("Requested read limit is outside the allowed range")


@dataclass(frozen=True, slots=True)
class LibraryPrefixRead:
    """Bounded binary prefix read without any interpretation of its content."""

    selected_library: SelectedLibrary
    requested_bytes: int
    data: bytes
    end_of_file: bool


def _validate_read_limit(max_bytes: int) -> None:
    if max_bytes <= 0 or max_bytes > MAX_LIBRARY_PROBE_BYTES:
        raise InvalidReadLimitError()


def read_library_prefix(
    selected_library: SelectedLibrary,
    max_bytes: int,
    *,
    source: LibraryBinarySource | None = None,
) -> LibraryPrefixRead:
    """Read at most a requested, bounded prefix from the selected library file.

    If the requested amount is filled, one additional byte is read solely to
    determine EOF; that byte is never returned to the caller.
    """
    if not isinstance(selected_library, SelectedLibrary):
        raise TypeError("selected_library must be a SelectedLibrary")
    _validate_read_limit(max_bytes)
    active_source = source if source is not None else _StandardLibraryBinarySource()
    try:
        with active_source.open_readonly(selected_library.candidate.absolute_path) as handle:
            data = bytes(handle.read(max_bytes))
            if len(data) < max_bytes:
                end_of_file = True
            else:
                end_of_file = handle.read(1) == b""
    except FileNotFoundError as exc:
        raise LibraryNotFoundError() from exc
    except PermissionError as exc:
        raise LibraryPermissionError() from exc
    except IsADirectoryError as exc:
        raise LibraryIsDirectoryError() from exc
    except OSError as exc:
        raise LibraryReadError() from exc
    return LibraryPrefixRead(
        selected_library=selected_library,
        requested_bytes=max_bytes,
        data=data,
        end_of_file=end_of_file,
    )
