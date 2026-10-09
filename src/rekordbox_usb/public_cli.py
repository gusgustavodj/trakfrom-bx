"""Public command surface for the Legacy-only executable core."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .public_runtime import PublicRuntime, PublicRuntimeError
from .public_watch import PublicWatchRuntime
from .usb_watch import PollingWindowsVolumeObserver
from .util import default_data_root


def main(argv: Sequence[str] | None = None, *, runtime: PublicRuntime | None = None,
         watch_factory=PublicWatchRuntime) -> int:
    parser = argparse.ArgumentParser(prog="trakfrom-bx")
    parser.add_argument("command", nargs="?", default="run",
                        choices=("run", "preflight", "publish", "status", "unpublish",
                                 "launch", "service", "install-shortcuts"),
                        help="default: monitor and publish all supported USB sources until Ctrl+C")
    parser.add_argument("--volume", type=Path, help="explicit source override; otherwise detect one Rekordbox USB")
    parser.add_argument("--data-root", type=Path, default=default_data_root(),
                        help="local managed data (default: existing RekordboxUSB user data directory)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--traktor-version", choices=("2", "3", "4"),
                        help="installed Traktor version to start with the supervised launcher")
    args = parser.parse_args(argv)
    if args.command in ("launch", "service", "install-shortcuts"):
        from .public_launcher import main as launcher_main
        forwarded = [args.command, "--data-root", str(args.data_root)]
        if args.traktor_version:
            forwarded += ["--traktor-version", args.traktor_version]
        return launcher_main(forwarded)
    active = runtime or PublicRuntime()
    try:
        if args.command == "run":
            def write_event(event):
                payload = event.to_dict()
                print(json.dumps(payload, sort_keys=True) if args.json else
                      " ".join(f"{key}={value}" for key, value in payload.items()), flush=True)
            options = {}
            if args.volume is not None:
                options["observer"] = PollingWindowsVolumeObserver(
                    lambda: [item for item in active._volumes()
                             if item.root == args.volume]
                )
            return watch_factory(active, args.data_root, event_sink=write_event, **options).run()
        args.volume = active.resolve_source(args.volume, args.data_root)
        if args.command == "preflight":
            result = active.preflight(args.volume, args.data_root)
            payload: dict[str, object] = {
                "command": args.command, "ready": result.ready, "code": result.code,
                "source_id": result.source_id,
            }
            exit_code = 0 if result.ready else (4 if result.code == "ELEVATION_REQUIRED" else 3)
        elif args.command == "publish":
            publication = active.publish(args.volume, args.data_root)
            payload = {
                "command": args.command, "code": "PUBLISHED",
                "source_id": publication.source_id,
                "playlist_count": publication.playlist_count,
                "track_count": publication.track_count,
                "changed": publication.changed,
                "mount_point": str(publication.mount_point) if publication.mount_point else None,
            }
            exit_code = 0
        else:
            status = (
                active.status(args.volume, args.data_root)
                if args.command == "status"
                else active.unpublish(args.volume, args.data_root)
            )
            payload = {
                "command": args.command, "code": status.state,
                "source_id": status.source_id, "attached": status.attached,
            }
            exit_code = 0
    except PublicRuntimeError as exc:
        payload = {"command": args.command, "code": exc.code}
        if exc.code == "MULTIPLE_REKORDBOX_USB":
            payload["message"] = "Multiple Rekordbox USB sources; select one with --volume <volume>."
        exit_code = 4 if exc.code == "ELEVATION_REQUIRED" else 3
    print(json.dumps(payload, sort_keys=True) if args.json else " ".join(f"{k}={v}" for k, v in payload.items()))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
