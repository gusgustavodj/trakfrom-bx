"""Setuptools backend with deterministic metadata for the generated sdist."""
from __future__ import annotations

import gzip
import io
import os
from pathlib import Path, PurePosixPath
import tarfile

from setuptools.build_meta import (
    build_wheel,
    get_requires_for_build_sdist,
    get_requires_for_build_wheel,
    prepare_metadata_for_build_wheel,
)
from setuptools.build_meta import build_sdist as _build_sdist


def build_sdist(sdist_directory, config_settings=None):
    name = _build_sdist(sdist_directory, config_settings)
    target = Path(sdist_directory) / name
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "1791504000"))
    if not 0 <= epoch <= 2**32 - 1:
        raise ValueError("SOURCE_DATE_EPOCH outside gzip timestamp range")
    output = io.BytesIO()
    with tarfile.open(target, "r:gz") as source:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for member in sorted(source.getmembers(), key=lambda item: item.name):
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                        raise ValueError("Unexpected generated sdist member")
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    member.mtime = epoch
                    member.mode = 0o755 if member.isdir() else 0o644
                    member.pax_headers = {}
                    archive.addfile(member, source.extractfile(member) if member.isfile() else None)
    target.write_bytes(output.getvalue())
    return name
