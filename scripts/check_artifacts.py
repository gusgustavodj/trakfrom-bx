"""Audit exact archive membership, metadata, LICENSE and source byte parity."""
from __future__ import annotations

from email.parser import BytesParser
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
import zipfile


def check(root, directory):
    public = set((root / "PUBLIC_FILES.txt").read_text().splitlines())
    wheels = list(directory.glob("*.whl"))
    sdists = list(directory.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1
    reports = []
    for path in [wheels[0], sdists[0]]:
        wheel = path.suffix == ".whl"
        if wheel:
            with zipfile.ZipFile(path) as archive:
                content = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
            prefix = "trakfrom_bx-0.1.0.dev0.dist-info/"
            mapped = {"src/" + name: data for name, data in content.items() if name.startswith("rekordbox_usb/")}
            expected = {name for name in public if name.startswith("src/")}
            metadata_names = {prefix + name for name in ("METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD", "licenses/LICENSE")}
            assert set(content) == {name[4:] for name in expected} | metadata_names
            license_data, metadata = content[prefix + "licenses/LICENSE"], content[prefix + "METADATA"]
        else:
            with tarfile.open(path) as archive:
                members = archive.getmembers()
                assert all(member.isfile() or member.isdir() for member in members)
                content = {member.name: archive.extractfile(member).read() for member in members if member.isfile()}
            prefix = "trakfrom_bx-0.1.0.dev0/"
            assert all(name.startswith(prefix) for name in content)
            logical = {name[len(prefix):]: data for name, data in content.items()}
            generated = {"PKG-INFO", "setup.cfg"} | {"src/trakfrom_bx.egg-info/" + name for name in ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "requires.txt", "top_level.txt")}
            assert public <= set(logical)
            assert set(logical) <= public | generated, sorted(set(logical) - public - generated)
            expected = public
            mapped = {name: logical[name] for name in expected}
            license_data, metadata = logical["LICENSE"], logical["PKG-INFO"]
        for name in content:
            assert not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts
        assert set(mapped) == expected
        assert all(data == (root / name).read_bytes() for name, data in mapped.items())
        assert license_data == (root / "LICENSE").read_bytes()
        meta = BytesParser().parsebytes(metadata)
        assert meta["Name"] == "trakfrom-bx" and meta["Version"] == "0.1.0.dev0"
        assert meta["License-Expression"] == "MIT" and meta["License-File"] == "LICENSE"
        assert meta.get_payload().replace("\r\n", "\n").strip() == (root / "README.md").read_text().strip()
        assert not [value for value in meta.get_all("Requires-Dist", []) if "extra ==" not in value]
        reports.append({"artifact": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "members": sorted(content), "source_files": len(mapped),
                        "license": "PASS", "metadata": "PASS", "byte_parity": "PASS"})
    return reports


if __name__ == "__main__":
    print(json.dumps(check(Path(__file__).resolve().parents[1], Path(sys.argv[1])), indent=2))
