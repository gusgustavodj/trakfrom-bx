"""Import without starting a service, touching hardware or probing the host."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys

from test_scope import CORE_MODULES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--core", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.installed:
        sys.path.insert(0, str(root / "src"))
    files = sorted((root / "src/rekordbox_usb").glob("*.py"))
    names = CORE_MODULES if args.core else [file.stem for file in files if file.stem != "__init__"]
    for name in ("", *names):
        module = importlib.import_module("rekordbox_usb" + ("." + name if name else ""))
        path = Path(module.__file__).resolve()
        if args.installed:
            assert "site-packages" in path.parts, str(path)
            assert path.read_bytes() == (root / "src/rekordbox_usb" / path.name).read_bytes()
    forbidden = ("rekordbox_usb_internal", "pyrekordbox", "sqlcipher3", "pysqlcipher3")
    assert not any(name == item or name.startswith(item + ".") for name in sys.modules for item in forbidden)
    print(json.dumps({"modules_imported": len(names) + 1, "installed": args.installed,
                      "private_modules_loaded": 0, "byte_parity": bool(args.installed)}))


if __name__ == "__main__":
    main()
