"""Unprivileged per-user preference initialization for the native Setup."""
import argparse
from pathlib import Path
import sys

from .desktop_preferences import DesktopSettings, PreferenceStore
from .util import default_data_root
from .windows_startup import WindowsStartup


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--startup", choices=("yes", "no", "preserve"), required=True)
    args = parser.parse_args(argv)
    import ctypes
    # Setup must run this helper with the original, normal user token.
    if ctypes.windll.shell32.IsUserAnAdmin():
        return 4
    root = default_data_root()
    from .public_launcher import validate_root
    validate_root(root)
    startup = WindowsStartup(Path(sys.executable).resolve().with_name("pythonw.exe"), root)
    enabled = startup.enabled() if args.startup == "preserve" else args.startup == "yes"
    DesktopSettings(PreferenceStore(root), startup).set_startup(enabled)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(3)
