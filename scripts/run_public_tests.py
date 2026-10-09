"""Run the same self-contained public suite from source or installed product."""
from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import sys
import tempfile

from test_scope import CORE_FILES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--core", action="store_true")
    parser.add_argument("--temp-root", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # A new directory every run preserves earlier evidence and avoids stale tests.
    parent = args.temp_root or Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="trakfrom-bx-", dir=parent))
    sys.path.insert(0, str(root))  # test helpers, never the src directory in installed mode
    if args.installed:
        os.environ.pop("PYTHONPATH", None)
        assert str(root / "src") not in sys.path
        module = importlib.import_module("rekordbox_usb")
        assert "site-packages" in Path(module.__file__).parts
        os.environ["TRAKTORBOX_TEST_EXECUTION_MODE"] = "installed"
        os.environ["TRAKTORBOX_TEST_EXTERNAL_CWD"] = str(work)
    else:
        sys.path.insert(0, str(root / "src"))
        os.environ["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root)))
        os.environ["TRAKTORBOX_TEST_EXECUTION_MODE"] = "source"
    config = work / "pytest.ini"
    config.write_text("[pytest]\nmarkers =\n    core_portable: explicit portable contract\n    windows_only: Windows contract or native boundary\n", encoding="utf-8")
    os.chdir(work)
    import pytest
    tests = sorted(root / name for name in CORE_FILES) if args.core else [root / "tests"]
    return pytest.main([*[str(path) for path in tests], "-q", "-p", "no:cacheprovider",
                        "-c", str(config), "--basetemp", str(work / "tmp")])


if __name__ == "__main__":
    raise SystemExit(main())
