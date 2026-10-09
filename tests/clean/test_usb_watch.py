from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rekordbox_usb.host_process import (
    HostProcessState,
    WindowsProcessEntry,
    WindowsTraktorProcessProbe,
)
from rekordbox_usb.usb_identity import UsbIdentityStore
from rekordbox_usb.usb_watch import (
    ActiveUsbPublication,
    ActivePublicationRegistry,
    SHUTDOWN_CLEANUP_OPERATIONAL_EXIT_CODE,
    StartupRecoveryDeferred,
    StartupRecoveryFailure,
    StartupRecoveryResult,
    StartupRecoveryUnpublication,
    WatchUsbConfig,
    WatchUsbEvent,
    WatchUsbRuntime,
)
from rekordbox_usb.windows_drives import (
    DriveInfo,
    PreflightStatus,
    RekordboxPreflightReport,
    RekordboxPreflightReportItem,
)


def _volume(
    root: str,
    *,
    label: str = "USB",
    drive_type: int | None = 2,
) -> DriveInfo:
    return DriveInfo(Path(root), label, root[0] * 8, "FAT32", drive_type)


def _report(
    volume: DriveInfo,
    status: PreflightStatus,
    *,
    warnings: tuple[str, ...] = (),
) -> RekordboxPreflightReport:
    return RekordboxPreflightReport(
        1,
        (
            RekordboxPreflightReportItem(
                str(volume.root),
                volume.label,
                volume.serial,
                volume.filesystem,
                volume.drive_type,
                status.value,
                warnings,
                (),
                (),
                status in {PreflightStatus.READY, PreflightStatus.READY_WITH_WARNINGS},
            ),
        ),
    )


class FakeObserver:
    def __init__(self, snapshots: list[tuple[DriveInfo, ...] | BaseException]) -> None:
        self.snapshots = snapshots
        self.index = 0
        self.closed = False

    def snapshot(self) -> tuple[DriveInfo, ...]:
        item = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeReadiness:
    def __init__(self, responses: dict[str, list[bool]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[str] = []

    def is_accessible(self, root: Path) -> bool:
        key = str(root)
        self.calls.append(key)
        values = self.responses.get(key)
        if not values:
            return True
        return values.pop(0)


class FakeHostProcessProbe:
    def __init__(
        self,
        states: list[HostProcessState | BaseException],
    ) -> None:
        self.states = states
        self.index = 0

    def state(self) -> HostProcessState:
        item = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        if isinstance(item, BaseException):
            raise item
        return item


def _published(volume: DriveInfo, *, changed: bool = True, warnings=()):
    return SimpleNamespace(
        status="PUBLISHED",
        changed=changed,
        identity_id=f"identity-{volume.root.drive}",
        source_id=f"rekordbox-usb:identity-{volume.root.drive}",
        vhdx_path=Path(f"C:\\managed\\{volume.root.drive[0].lower()}.vhdx"),
        vhdx_mount=Path("Q:\\"),
        warnings=warnings,
    )


def _stable_published(volume: DriveInfo, *, changed: bool = True):
    identity_id = UsbIdentityStore._identity_id(volume.serial)
    return SimpleNamespace(
        status="PUBLISHED",
        changed=changed,
        identity_id=identity_id,
        source_id=f"rekordbox-usb:{identity_id}",
        vhdx_path=Path(f"C:\\managed\\{identity_id}.vhdx"),
        vhdx_mount=Path("Q:\\"),
        warnings=(),
    )


def _shutdown_result(
    publication: ActiveUsbPublication,
    *,
    changed: bool,
    mount_before: Path | None = Path("Q:\\"),
):
    return SimpleNamespace(
        changed=changed,
        identity_id=publication.identity_id,
        source_id=publication.source_id,
        vhdx_path=publication.vhdx_path,
        vhdx_mount_before=mount_before,
        vhdx_mount_after=None,
    )


def _run(
    snapshots: list[tuple[DriveInfo, ...] | BaseException],
    *,
    statuses: dict[str, PreflightStatus] | None = None,
    warnings: dict[str, tuple[str, ...]] | None = None,
    publish_existing: bool = False,
    debounce: float = 0.0,
    readiness: FakeReadiness | None = None,
    readiness_attempts: int = 3,
    publisher=None,
    unpublisher=None,
    publication_registry: ActivePublicationRegistry | None = None,
    startup_recoverer=None,
    host_process_probe=None,
    cycles: int = 1,
):
    observer = FakeObserver(snapshots)
    clock = FakeClock()
    events: list[WatchUsbEvent] = []
    publish_calls: list[str] = []
    by_root = {
        str(volume.root): volume
        for snapshot in snapshots
        if isinstance(snapshot, tuple)
        for volume in snapshot
    }

    def preflight(*, volume_root):
        key = str(volume_root)
        return _report(
            by_root[key],
            (statuses or {}).get(key, PreflightStatus.READY),
            warnings=(warnings or {}).get(key, ()),
        )

    def publish(*, volume_root):
        key = str(volume_root)
        publish_calls.append(key)
        if publisher is not None:
            return publisher(by_root[key])
        return _published(by_root[key])

    runtime = WatchUsbRuntime(
        observer=observer,
        preflight_runner=preflight,
        publisher=publish,
        unpublisher=unpublisher,
        event_sink=events.append,
        config=WatchUsbConfig(
            poll_interval_seconds=1.0,
            debounce_seconds=debounce,
            readiness_attempts=readiness_attempts,
            readiness_initial_delay_seconds=0.25,
            publish_existing=publish_existing,
        ),
        readiness_probe=readiness or FakeReadiness(),
        publication_registry=publication_registry,
        clock=clock,
        sleep=clock.sleep,
        startup_recoverer=startup_recoverer,
        host_process_probe=host_process_probe,
    )
    result = runtime.run(max_cycles=cycles)
    return result, observer, clock, events, publish_calls


def _event_names(events: list[WatchUsbEvent]) -> list[str]:
    return [item.event for item in events]


def test_initial_snapshot_is_known_but_not_an_arrival_by_default() -> None:
    volume = _volume("D:\\")
    _result, _observer, _clock, events, calls = _run([(volume,)], cycles=1)

    assert "VOLUME_KNOWN" in _event_names(events)
    assert "VOLUME_ARRIVED" not in _event_names(events)
    assert calls == []


def test_empty_startup_recovery_completes_before_normal_watching() -> None:
    volume = _volume("D:\\")
    recovery_calls = []

    def recover(*, volumes, host_state):
        recovery_calls.append((volumes, host_state))
        return StartupRecoveryResult()

    _result, _observer, _clock, events, calls = _run(
        [(volume,)],
        startup_recoverer=recover,
        cycles=1,
    )

    assert recovery_calls == [
        ((volume,), HostProcessState.NOT_RUNNING)
    ]
    assert calls == []
    assert _event_names(events)[:3] == [
        "STARTUP_RECOVERY_STARTED",
        "STARTUP_RECOVERY_COMPLETED",
        "WATCH_STARTED",
    ]
    assert events[1].details == {
        "reconciled": 0,
        "unpublished": 0,
        "errors": 0,
        "deferred": 0,
    }


def test_startup_recovery_rebuilds_registry_without_republishing_existing_usb() -> None:
    volume = _volume("F:\\")
    publication = ActiveUsbPublication(
        source_volume="f:\\",
        volume="F:\\",
        identity_id="stable-identity",
        source_id="rekordbox-usb:stable-identity",
        vhdx_path=Path("C:\\managed\\stable.vhdx"),
        vhdx_mount=Path("Q:\\"),
    )

    _result, _observer, _clock, events, calls = _run(
        [(volume,)],
        publish_existing=True,
        startup_recoverer=lambda **_kwargs: StartupRecoveryResult(
            reconciled=(publication,)
        ),
        cycles=1,
    )

    assert calls == []
    recovered = next(
        item for item in events if item.event == "MANAGED_VHDX_RECONCILED"
    )
    assert recovered.to_dict() == {
        "event": "MANAGED_VHDX_RECONCILED",
        "volume": "F:\\",
        "identity_id": "stable-identity",
        "source_id": "rekordbox-usb:stable-identity",
        "vhdx_mount": "Q:\\",
        "vhdx_path": "C:\\managed\\stable.vhdx",
    }


def test_removal_after_startup_recovery_uses_rebuilt_registry() -> None:
    volume = _volume("F:\\")
    publication = ActiveUsbPublication(
        source_volume="f:\\",
        volume="F:\\",
        identity_id="stable-identity",
        source_id="rekordbox-usb:stable-identity",
        vhdx_path=Path("C:\\managed\\stable.vhdx"),
        vhdx_mount=Path("Q:\\"),
    )
    unpublish_calls = []

    def unpublish(*, publication):
        unpublish_calls.append(publication)
        return SimpleNamespace(
            changed=True,
            vhdx_mount_before=Path("Q:\\"),
            vhdx_mount_after=None,
        )

    _run(
        [(volume,), ()],
        startup_recoverer=lambda **_kwargs: StartupRecoveryResult(
            reconciled=(publication,)
        ),
        unpublisher=unpublish,
        cycles=1,
    )

    assert unpublish_calls == [publication]


def test_startup_recovery_emits_independent_results_and_structured_errors() -> None:
    publication = ActiveUsbPublication(
        source_volume="d:\\",
        volume="D:\\",
        identity_id="present",
        source_id="rekordbox-usb:present",
        vhdx_path=Path("C:\\managed\\present.vhdx"),
        vhdx_mount=Path("Q:\\"),
    )
    result = StartupRecoveryResult(
        reconciled=(publication,),
        unpublished=(
            StartupRecoveryUnpublication(
                "orphan",
                "rekordbox-usb:orphan",
                Path("C:\\managed\\orphan.vhdx"),
                Path("R:\\"),
                None,
                True,
            ),
        ),
        errors=(
            StartupRecoveryFailure(
                Path("C:\\managed\\held.vhdx"),
                "WindowsVhdxDetachError",
                "held by Traktor",
                {"cause_type": "OSError"},
            ),
        ),
    )

    _result, _observer, _clock, events, _calls = _run(
        [()],
        startup_recoverer=lambda **_kwargs: result,
        cycles=1,
    )

    orphaned = next(
        item for item in events if item.event == "ORPHANED_VHDX_UNPUBLISHED"
    )
    assert orphaned.details["changed"] is True
    assert orphaned.details["vhdx_mount_after"] is None
    error = next(item for item in events if item.event == "STARTUP_RECOVERY_ERROR")
    assert error.details["cause_type"] == "OSError"
    completed = next(
        item for item in events if item.event == "STARTUP_RECOVERY_COMPLETED"
    )
    assert completed.details == {
        "reconciled": 1,
        "unpublished": 1,
        "errors": 1,
        "deferred": 0,
    }


def test_startup_recovery_failure_is_non_fatal_to_normal_arrivals() -> None:
    volume = _volume("D:\\")

    def fail_recovery(**_kwargs):
        raise OSError("storage inventory unavailable")

    _result, observer, _clock, events, calls = _run(
        [(), (volume,)],
        startup_recoverer=fail_recovery,
        cycles=1,
    )

    assert calls == ["D:\\"]
    assert observer.closed is True
    error = next(item for item in events if item.event == "STARTUP_RECOVERY_ERROR")
    assert error.details["error"] == "OSError"
    assert error.details["message"] == "storage inventory unavailable"
    assert "WATCH_STARTED" in _event_names(events)


def test_startup_deferred_orphan_is_retried_after_traktor_closes() -> None:
    path = Path("C:\\managed\\orphan.vhdx")
    calls = []

    def recover(*, volumes, host_state):
        calls.append((volumes, host_state))
        if host_state is HostProcessState.RUNNING:
            return StartupRecoveryResult(
                deferred=(
                    StartupRecoveryDeferred(
                        "identity",
                        "rekordbox-usb:identity",
                        path,
                        Path("Q:\\"),
                        "TRAKTOR_RUNNING",
                    ),
                )
            )
        return StartupRecoveryResult(
            unpublished=(
                StartupRecoveryUnpublication(
                    "identity",
                    "rekordbox-usb:identity",
                    path,
                    Path("Q:\\"),
                    None,
                    True,
                ),
            )
        )

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (), ()],
        startup_recoverer=recover,
        host_process_probe=FakeHostProcessProbe(
            [
                HostProcessState.RUNNING,
                HostProcessState.RUNNING,
                HostProcessState.NOT_RUNNING,
            ]
        ),
        cycles=2,
    )

    assert [state for _volumes, state in calls] == [
        HostProcessState.RUNNING,
        HostProcessState.NOT_RUNNING,
    ]
    assert _event_names(events).count("STARTUP_RECOVERY_DEFERRED") == 1
    assert _event_names(events).count("ORPHANED_VHDX_UNPUBLISHED") == 1


def test_publish_existing_processes_the_initial_snapshot_explicitly() -> None:
    volume = _volume("D:\\")
    _result, _observer, _clock, events, calls = _run(
        [(volume,)],
        publish_existing=True,
        cycles=1,
    )

    assert next(item for item in events if item.event == "VOLUME_ARRIVED").details == {
        "existing": True
    }
    assert calls == ["D:\\"]


def test_new_ready_volume_publishes_once_for_the_exact_mount() -> None:
    volume = _volume("D:\\")
    _result, _observer, _clock, events, calls = _run([(), (volume,)], cycles=1)

    assert calls == ["D:\\"]
    published = next(item for item in events if item.event == "USB_PUBLISHED")
    assert published.volume == "D:\\"
    assert published.details["status"] == "PUBLISHED"


def test_ready_with_warnings_publishes_and_preserves_warnings() -> None:
    volume = _volume("D:\\")
    _result, _observer, _clock, events, calls = _run(
        [(), (volume,)],
        statuses={"D:\\": PreflightStatus.READY_WITH_WARNINGS},
        warnings={"D:\\": ("partial marker access",)},
        cycles=1,
    )

    assert calls == ["D:\\"]
    started = next(item for item in events if item.event == "USB_PUBLICATION_STARTED")
    assert started.details["warnings"] == ["partial marker access"]


def test_common_volume_is_ignored_without_publication() -> None:
    volume = _volume("F:\\")
    _result, _observer, _clock, events, calls = _run(
        [(), (volume,)],
        statuses={"F:\\": PreflightStatus.UNSUPPORTED},
        cycles=1,
    )

    assert calls == []
    ignored = next(item for item in events if item.event == "VOLUME_IGNORED")
    assert ignored.details["reason"] == "NOT_REKORDBOX_USB"


def test_blocked_volume_is_ignored_with_preflight_reason() -> None:
    volume = _volume("F:\\")
    _result, _observer, _clock, events, calls = _run(
        [(), (volume,)],
        statuses={"F:\\": PreflightStatus.BLOCKED},
        cycles=1,
    )

    assert calls == []
    ignored = next(item for item in events if item.event == "VOLUME_IGNORED")
    assert ignored.details["reason"] == "PREFLIGHT_BLOCKED"


def test_fixed_internal_volume_is_preflighted_but_never_published() -> None:
    volume = _volume("C:\\", label="SYSTEM", drive_type=3)
    _result, _observer, _clock, events, calls = _run([(), (volume,)], cycles=1)

    assert calls == []
    ignored = next(item for item in events if item.event == "VOLUME_IGNORED")
    assert ignored.details["reason"] == "INTERNAL_DISK"


def test_managed_vhdx_arriving_during_publish_does_not_recurse() -> None:
    usb = _volume("D:\\")
    vhdx = _volume("Q:\\", label="TRAKTORBOX", drive_type=3)
    _result, _observer, _clock, events, calls = _run(
        [(), (usb,), (usb, vhdx)],
        cycles=2,
    )

    assert calls == ["D:\\"]
    ignored = [
        item for item in events if item.event == "VOLUME_IGNORED" and item.volume == "Q:\\"
    ]
    assert ignored[0].details["reason"] == "MANAGED_VHDX"


def test_duplicate_snapshot_entries_coalesce_to_one_logical_attempt() -> None:
    volume = _volume("D:\\")
    _result, _observer, _clock, _events, calls = _run(
        [(), (volume, volume), (volume,)],
        debounce=1.0,
        cycles=2,
    )

    assert calls == ["D:\\"]


def test_not_ready_volume_retries_then_publishes() -> None:
    volume = _volume("D:\\")
    readiness = FakeReadiness({"D:\\": [False, False, True]})
    _result, _observer, clock, _events, calls = _run(
        [(), (volume,)],
        readiness=readiness,
        cycles=1,
    )

    assert calls == ["D:\\"]
    assert readiness.calls == ["D:\\", "D:\\", "D:\\"]
    assert clock.sleeps == [0.25, 0.5]


def test_never_ready_volume_emits_error_and_watcher_processes_the_next_arrival() -> None:
    first = _volume("D:\\")
    second = _volume("F:\\")
    readiness = FakeReadiness(
        {
            "D:\\": [False, False, False],
            "F:\\": [True],
        }
    )
    _result, _observer, _clock, events, calls = _run(
        [(), (first,), (first, second)],
        readiness=readiness,
        cycles=2,
    )

    assert calls == ["F:\\"]
    error = next(item for item in events if item.event == "VOLUME_ERROR")
    assert error.volume == "D:\\"
    assert error.details["reason"] == "VOLUME_NOT_READY"
    assert "USB_PUBLISHED" in _event_names(events)


def test_volume_disappearing_during_debounce_is_not_published() -> None:
    volume = _volume("D:\\")
    _result, _observer, _clock, events, calls = _run(
        [(), (volume,), ()],
        debounce=1.0,
        cycles=2,
    )

    assert calls == []
    ignored = next(item for item in events if item.event == "VOLUME_IGNORED")
    assert ignored.details["reason"] == "VOLUME_DISAPPEARED"


def test_publish_failure_does_not_stop_a_later_independent_arrival() -> None:
    first = _volume("D:\\")
    second = _volume("F:\\")

    def publish(volume: DriveInfo):
        if str(volume.root) == "D:\\":
            raise RuntimeError("publication failed")
        return _published(volume)

    _result, _observer, _clock, events, calls = _run(
        [(), (first,), (first, second)],
        publisher=publish,
        cycles=2,
    )

    assert calls == ["D:\\", "F:\\"]
    error = next(item for item in events if item.event == "VOLUME_ERROR")
    assert error.details["reason"] == "PUBLISH_FAILED"
    assert any(item.event == "USB_PUBLISHED" and item.volume == "F:\\" for item in events)


def test_two_arrivals_use_independent_explicit_mounts() -> None:
    first = _volume("D:\\")
    second = _volume("F:\\")
    _result, _observer, _clock, _events, calls = _run(
        [(), (first, second)],
        cycles=1,
    )

    assert calls == ["D:\\", "F:\\"]


def test_ctrl_c_closes_observer_and_emits_clean_stop() -> None:
    observer = FakeObserver([(), KeyboardInterrupt()])
    events: list[WatchUsbEvent] = []
    runtime = WatchUsbRuntime(
        observer=observer,
        preflight_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        publisher=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        event_sink=events.append,
        config=WatchUsbConfig(debounce_seconds=0),
    )

    assert runtime.run() == 0
    assert observer.closed is True
    assert _event_names(events) == [
        "WATCH_STARTED",
        "SHUTDOWN_CLEANUP_STARTED",
        "SHUTDOWN_CLEANUP_COMPLETED",
        "WATCH_STOPPED",
    ]
    assert events[-2].details == {
        "cleaned": 0,
        "already_detached": 0,
        "errors": 0,
        "deferred": 0,
    }


def test_ctrl_c_after_startup_recovery_closes_observer_cleanly() -> None:
    observer = FakeObserver([(), KeyboardInterrupt()])
    events: list[WatchUsbEvent] = []
    runtime = WatchUsbRuntime(
        observer=observer,
        preflight_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        publisher=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        event_sink=events.append,
        startup_recoverer=lambda **_kwargs: StartupRecoveryResult(),
        config=WatchUsbConfig(debounce_seconds=0),
    )

    assert runtime.run() == 0
    assert observer.closed is True
    assert _event_names(events) == [
        "STARTUP_RECOVERY_STARTED",
        "STARTUP_RECOVERY_COMPLETED",
        "WATCH_STARTED",
        "SHUTDOWN_CLEANUP_STARTED",
        "SHUTDOWN_CLEANUP_COMPLETED",
        "WATCH_STOPPED",
    ]


def test_ctrl_c_detaches_one_active_publication_after_observer_close() -> None:
    observer = FakeObserver([(), KeyboardInterrupt()])
    registry = ActivePublicationRegistry()
    publication = ActiveUsbPublication(
        source_volume="d:\\",
        volume="D:\\",
        identity_id="identity",
        source_id="rekordbox-usb:identity",
        vhdx_path=Path("C:\\managed\\identity.vhdx"),
        vhdx_mount=Path("Q:\\"),
    )
    registry.register(publication)
    events: list[WatchUsbEvent] = []
    calls = []

    def shutdown_unpublish(*, publication):
        assert observer.closed is True
        calls.append(publication)
        return _shutdown_result(publication, changed=True)

    runtime = WatchUsbRuntime(
        observer=observer,
        preflight_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        publisher=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        event_sink=events.append,
        publication_registry=registry,
        shutdown_unpublisher=shutdown_unpublish,
        host_process_probe=FakeHostProcessProbe(
            [HostProcessState.NOT_RUNNING]
        ),
    )

    assert runtime.run() == 0
    assert calls == [publication]
    assert registry.snapshot() == ()
    assert _event_names(events)[-5:] == [
        "SHUTDOWN_CLEANUP_STARTED",
        "SHUTDOWN_UNPUBLICATION_STARTED",
        "SHUTDOWN_UNPUBLISHED",
        "SHUTDOWN_CLEANUP_COMPLETED",
        "WATCH_STOPPED",
    ]
    unpublished = events[-3]
    assert unpublished.details["changed"] is True
    assert unpublished.details["vhdx_path"] == str(publication.vhdx_path)
    assert unpublished.details["vhdx_mount_before"] == "Q:\\"
    assert events[-2].details == {
        "cleaned": 1,
        "already_detached": 0,
        "errors": 0,
        "deferred": 0,
    }


def test_ctrl_c_with_legacy_traktor_defers_cleanup_and_stops_last() -> None:
    observer = FakeObserver([(), KeyboardInterrupt()])
    registry = ActivePublicationRegistry()
    publication = ActiveUsbPublication(
        "d:\\",
        "D:\\",
        "identity",
        "rekordbox-usb:identity",
        Path("C:\\managed\\identity.vhdx"),
        Path("Q:\\"),
    )
    registry.register(publication)
    calls = []
    events: list[WatchUsbEvent] = []
    runtime = WatchUsbRuntime(
        observer=observer,
        preflight_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        publisher=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        event_sink=events.append,
        publication_registry=registry,
        shutdown_unpublisher=lambda **kwargs: calls.append(kwargs),
        host_process_probe=WindowsTraktorProcessProbe(
            lambda: (WindowsProcessEntry(42, "Traktor.exe"),),
            lambda _pid: r"C:\Program Files\Native Instruments\Traktor Pro 3\Traktor.exe",
        ),
    )

    assert runtime.run() == 0
    assert calls == []
    assert registry.snapshot() == (publication,)
    deferred = next(
        item for item in events if item.event == "SHUTDOWN_CLEANUP_DEFERRED"
    )
    assert deferred.details["reason"] == "TRAKTOR_RUNNING"
    assert deferred.details["vhdx_path"] == str(publication.vhdx_path)
    assert events[-2].details == {
        "cleaned": 0,
        "already_detached": 0,
        "errors": 0,
        "deferred": 1,
    }
    assert events[-1].event == "WATCH_STOPPED"
    assert "SHUTDOWN_UNPUBLISHED" not in _event_names(events)


def test_ctrl_c_with_unknown_host_state_preserves_vhdx_and_returns_nonzero() -> None:
    observer = FakeObserver([(), KeyboardInterrupt()])
    registry = ActivePublicationRegistry()
    publication = ActiveUsbPublication(
        "d:\\",
        "D:\\",
        "identity",
        "rekordbox-usb:identity",
        Path("C:\\managed\\identity.vhdx"),
        Path("Q:\\"),
    )
    registry.register(publication)
    calls = []
    events: list[WatchUsbEvent] = []
    runtime = WatchUsbRuntime(
        observer=observer,
        preflight_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        publisher=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        event_sink=events.append,
        publication_registry=registry,
        shutdown_unpublisher=lambda **kwargs: calls.append(kwargs),
        host_process_probe=FakeHostProcessProbe(
            [OSError("process inventory unavailable")]
        ),
    )

    assert runtime.run() == SHUTDOWN_CLEANUP_OPERATIONAL_EXIT_CODE
    assert calls == []
    assert registry.snapshot() == (publication,)
    deferred = next(
        item for item in events if item.event == "SHUTDOWN_CLEANUP_DEFERRED"
    )
    assert deferred.details["reason"] == "HOST_STATE_UNKNOWN"
    assert events[-2].details == {
        "cleaned": 0,
        "already_detached": 0,
        "errors": 1,
        "deferred": 1,
    }
    assert events[-1].event == "WATCH_STOPPED"


def test_ctrl_c_already_detached_publication_is_idempotent() -> None:
    observer = FakeObserver([(), KeyboardInterrupt()])
    registry = ActivePublicationRegistry()
    publication = ActiveUsbPublication(
        source_volume="d:\\",
        volume="D:\\",
        identity_id="identity",
        source_id="rekordbox-usb:identity",
        vhdx_path=Path("C:\\managed\\identity.vhdx"),
        vhdx_mount=None,
    )
    registry.register(publication)
    events: list[WatchUsbEvent] = []
    runtime = WatchUsbRuntime(
        observer=observer,
        preflight_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        publisher=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        event_sink=events.append,
        publication_registry=registry,
        shutdown_unpublisher=lambda *, publication: _shutdown_result(
            publication,
            changed=False,
            mount_before=None,
        ),
    )

    assert runtime.run() == 0
    assert registry.snapshot() == ()
    assert events[-3].details["changed"] is False
    assert events[-2].details == {
        "cleaned": 0,
        "already_detached": 1,
        "errors": 0,
        "deferred": 0,
    }


def test_observer_close_failure_still_cleans_registry_and_stops_last() -> None:
    class CloseFailingObserver(FakeObserver):
        def close(self) -> None:
            self.closed = True
            raise OSError("observer close failed")

    observer = CloseFailingObserver([(), KeyboardInterrupt()])
    registry = ActivePublicationRegistry()
    publication = ActiveUsbPublication(
        "d:\\",
        "D:\\",
        "identity",
        "rekordbox-usb:identity",
        Path("C:\\managed\\identity.vhdx"),
        Path("Q:\\"),
    )
    registry.register(publication)
    events: list[WatchUsbEvent] = []
    runtime = WatchUsbRuntime(
        observer=observer,
        preflight_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        publisher=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        event_sink=events.append,
        publication_registry=registry,
        shutdown_unpublisher=lambda *, publication: _shutdown_result(
            publication,
            changed=True,
        ),
    )

    assert runtime.run() == SHUTDOWN_CLEANUP_OPERATIONAL_EXIT_CODE
    assert observer.closed is True
    assert registry.snapshot() == ()
    error = next(item for item in events if item.event == "SHUTDOWN_CLEANUP_ERROR")
    assert error.details["error"] == "OSError"
    assert error.details["message"] == "observer close failed"
    assert events[-2].details == {
        "cleaned": 1,
        "already_detached": 0,
        "errors": 1,
        "deferred": 0,
    }
    assert events[-1].event == "WATCH_STOPPED"


def test_shutdown_failure_keeps_failed_registry_entry_and_processes_the_rest() -> None:
    observer = FakeObserver([(), KeyboardInterrupt()])
    registry = ActivePublicationRegistry()
    first = ActiveUsbPublication(
        "d:\\",
        "D:\\",
        "first",
        "rekordbox-usb:first",
        Path("C:\\managed\\first.vhdx"),
        Path("Q:\\"),
    )
    second = ActiveUsbPublication(
        "f:\\",
        "F:\\",
        "second",
        "rekordbox-usb:second",
        Path("C:\\managed\\second.vhdx"),
        None,
    )
    registry.register(second)
    registry.register(first)
    calls = []
    events: list[WatchUsbEvent] = []

    def shutdown_unpublish(*, publication):
        calls.append(publication.identity_id)
        if publication is first:
            raise OSError("held by Traktor")
        return _shutdown_result(publication, changed=False, mount_before=None)

    runtime = WatchUsbRuntime(
        observer=observer,
        preflight_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        publisher=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        event_sink=events.append,
        publication_registry=registry,
        shutdown_unpublisher=shutdown_unpublish,
    )

    assert runtime.run() == SHUTDOWN_CLEANUP_OPERATIONAL_EXIT_CODE
    assert calls == ["first", "second"]
    assert registry.snapshot() == (first,)
    error = next(item for item in events if item.event == "SHUTDOWN_CLEANUP_ERROR")
    assert error.details["error"] == "OSError"
    assert error.details["message"] == "held by Traktor"
    assert events[-2].details == {
        "cleaned": 0,
        "already_detached": 1,
        "errors": 1,
        "deferred": 0,
    }
    assert events[-1].event == "WATCH_STOPPED"


def test_divergent_registry_entry_is_not_passed_to_shutdown_unpublisher() -> None:
    observer = FakeObserver([(), KeyboardInterrupt()])
    registry = ActivePublicationRegistry()
    divergent = ActiveUsbPublication(
        "d:\\",
        "D:\\",
        "identity",
        "rekordbox-usb:different",
        Path("C:\\managed\\identity.vhdx"),
        Path("Q:\\"),
    )
    registry.register(divergent)
    calls = []
    events: list[WatchUsbEvent] = []
    runtime = WatchUsbRuntime(
        observer=observer,
        preflight_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        publisher=lambda **_kwargs: calls.append("publish"),
        event_sink=events.append,
        publication_registry=registry,
        shutdown_unpublisher=lambda **_kwargs: calls.append("unpublish"),
    )

    assert runtime.run() == SHUTDOWN_CLEANUP_OPERATIONAL_EXIT_CODE
    assert calls == []
    assert registry.snapshot() == (divergent,)
    assert events[-2].details["errors"] == 1
    assert events[-1].event == "WATCH_STOPPED"


def test_abrupt_runtime_failure_closes_observer_without_shutdown_cleanup() -> None:
    observer = FakeObserver([(), RuntimeError("inventory failed")])
    registry = ActivePublicationRegistry()
    publication = ActiveUsbPublication(
        "d:\\",
        "D:\\",
        "identity",
        "rekordbox-usb:identity",
        Path("C:\\managed\\identity.vhdx"),
        Path("Q:\\"),
    )
    registry.register(publication)
    calls = []
    events: list[WatchUsbEvent] = []
    runtime = WatchUsbRuntime(
        observer=observer,
        preflight_runner=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        publisher=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        event_sink=events.append,
        publication_registry=registry,
        shutdown_unpublisher=lambda **_kwargs: calls.append("unpublish"),
    )

    with pytest.raises(RuntimeError, match="inventory failed"):
        runtime.run()

    assert observer.closed is True
    assert calls == []
    assert not any(
        item.event.startswith("SHUTDOWN_")
        for item in events
    )
    assert events[-1].event == "WATCH_STOPPED"


def test_successful_publish_registers_the_complete_managed_reference() -> None:
    volume = _volume("D:\\")
    registry = ActivePublicationRegistry()

    _run([(), (volume,)], publication_registry=registry, cycles=1)

    assert len(registry.snapshot()) == 1
    publication = registry.snapshot()[0]
    assert publication.source_volume == "d:\\"
    assert publication.volume == "D:\\"
    assert publication.identity_id == "identity-D:"
    assert publication.source_id == "rekordbox-usb:identity-D:"
    assert publication.vhdx_path == Path("C:\\managed\\d.vhdx")
    assert publication.vhdx_mount == Path("Q:\\")


def test_removed_published_usb_unpublishes_exact_path_and_clears_registry() -> None:
    volume = _volume("D:\\")
    registry = ActivePublicationRegistry()
    calls = []

    def unpublish(*, publication):
        calls.append(publication)
        return SimpleNamespace(
            changed=True,
            vhdx_mount_before=Path("Q:\\"),
            vhdx_mount_after=None,
        )

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (volume,), ()],
        unpublisher=unpublish,
        publication_registry=registry,
        host_process_probe=FakeHostProcessProbe(
            [HostProcessState.NOT_RUNNING]
        ),
        cycles=2,
    )

    assert [item.vhdx_path for item in calls] == [Path("C:\\managed\\d.vhdx")]
    assert registry.snapshot() == ()
    assert "VOLUME_REMOVED" in _event_names(events)
    unpublished = next(item for item in events if item.event == "USB_UNPUBLISHED")
    assert unpublished.details["changed"] is True
    assert unpublished.details["identity_id"] == "identity-D:"
    assert unpublished.details["source_id"] == "rekordbox-usb:identity-D:"
    assert unpublished.details["vhdx_path"] == "C:\\managed\\d.vhdx"
    assert unpublished.details["vhdx_mount_before"] == "Q:\\"
    assert unpublished.details["vhdx_mount_after"] is None


def test_running_traktor_defers_removal_without_detach() -> None:
    volume = _volume("D:\\")
    registry = ActivePublicationRegistry()
    unpublish_calls = []

    _result, _observer, _clock, events, publish_calls = _run(
        [(), (volume,), ()],
        publisher=_stable_published,
        unpublisher=lambda **kwargs: unpublish_calls.append(kwargs),
        publication_registry=registry,
        host_process_probe=FakeHostProcessProbe(
            [HostProcessState.RUNNING]
        ),
        cycles=2,
    )

    assert publish_calls == ["D:\\"]
    assert unpublish_calls == []
    assert len(registry.snapshot()) == 1
    deferred = next(
        item for item in events if item.event == "USB_UNPUBLICATION_DEFERRED"
    )
    publication = registry.snapshot()[0]
    assert deferred.to_dict() == {
        "event": "USB_UNPUBLICATION_DEFERRED",
        "volume": "D:\\",
        "reason": "TRAKTOR_RUNNING",
        "identity_id": publication.identity_id,
        "source_id": publication.source_id,
        "vhdx_mount": "Q:\\",
        "vhdx_path": str(publication.vhdx_path),
    }
    assert "USB_UNPUBLICATION_STARTED" not in _event_names(events)


def test_same_usb_return_cancels_deferred_without_attach_or_duplicate_publish() -> None:
    volume = _volume("D:\\")
    registry = ActivePublicationRegistry()
    unpublish_calls = []

    _result, _observer, _clock, events, publish_calls = _run(
        [(), (volume,), (), (volume,)],
        publisher=_stable_published,
        unpublisher=lambda **kwargs: unpublish_calls.append(kwargs),
        publication_registry=registry,
        host_process_probe=FakeHostProcessProbe(
            [HostProcessState.RUNNING]
        ),
        cycles=3,
    )

    assert publish_calls == ["D:\\"]
    assert unpublish_calls == []
    assert len(registry.snapshot()) == 1
    restored = next(item for item in events if item.event == "USB_SOURCE_RESTORED")
    assert restored.volume == "D:\\"
    assert restored.details["identity_id"] == registry.snapshot()[0].identity_id
    assert len(
        [item for item in events if item.event == "USB_UNPUBLICATION_DEFERRED"]
    ) == 1


def test_same_usb_return_at_new_letter_defers_catalog_update() -> None:
    original = _volume("D:\\")
    relocated = DriveInfo(
        Path("F:\\"),
        original.label,
        original.serial,
        original.filesystem,
        original.drive_type,
    )
    registry = ActivePublicationRegistry()
    unpublish_calls = []

    _result, _observer, _clock, events, publish_calls = _run(
        [(), (original,), (), (relocated,)],
        publisher=_stable_published,
        unpublisher=lambda **kwargs: unpublish_calls.append(kwargs),
        publication_registry=registry,
        host_process_probe=FakeHostProcessProbe(
            [HostProcessState.RUNNING]
        ),
        cycles=3,
    )

    assert publish_calls == ["D:\\"]
    assert unpublish_calls == []
    assert len(registry.snapshot()) == 1
    relocation = next(
        item
        for item in events
        if item.event == "USB_SOURCE_RELOCATION_DEFERRED"
    )
    assert relocation.volume == "F:\\"
    assert relocation.details["previous_volume"] == "D:\\"
    assert "USB_SOURCE_RESTORED" not in _event_names(events)


def test_new_letter_catalog_is_republished_only_after_traktor_closes() -> None:
    original = _volume("D:\\")
    relocated = DriveInfo(
        Path("F:\\"),
        original.label,
        original.serial,
        original.filesystem,
        original.drive_type,
    )
    registry = ActivePublicationRegistry()
    unpublish_calls = []

    def unpublish(*, publication):
        unpublish_calls.append(publication)
        return SimpleNamespace(
            changed=True,
            vhdx_mount_before=Path("Q:\\"),
            vhdx_mount_after=None,
        )

    _result, _observer, _clock, events, publish_calls = _run(
        [(), (original,), (), (relocated,)],
        publisher=_stable_published,
        unpublisher=unpublish,
        publication_registry=registry,
        host_process_probe=FakeHostProcessProbe(
            [
                HostProcessState.RUNNING,
                HostProcessState.RUNNING,
                HostProcessState.NOT_RUNNING,
            ]
        ),
        cycles=3,
    )

    assert publish_calls == ["D:\\", "F:\\"]
    assert len(unpublish_calls) == 1
    assert len(registry.snapshot()) == 1
    assert registry.snapshot()[0].source_volume == "f:\\"
    unpublish_index = _event_names(events).index("USB_UNPUBLISHED")
    republish_index = next(
        index
        for index, item in enumerate(events)
        if item.event == "USB_PUBLICATION_STARTED" and item.volume == "F:\\"
    )
    assert unpublish_index < republish_index


def test_deferred_unpublish_runs_once_after_traktor_closes_with_usb_absent() -> None:
    volume = _volume("D:\\")
    registry = ActivePublicationRegistry()
    unpublish_calls = []

    def unpublish(*, publication):
        unpublish_calls.append(publication)
        return SimpleNamespace(
            changed=True,
            vhdx_mount_before=Path("Q:\\"),
            vhdx_mount_after=None,
        )

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (volume,), (), ()],
        publisher=_stable_published,
        unpublisher=unpublish,
        publication_registry=registry,
        host_process_probe=FakeHostProcessProbe(
            [
                HostProcessState.RUNNING,
                HostProcessState.RUNNING,
                HostProcessState.NOT_RUNNING,
            ]
        ),
        cycles=3,
    )

    assert len(unpublish_calls) == 1
    assert registry.snapshot() == ()
    assert _event_names(events).count("USB_UNPUBLICATION_STARTED") == 1
    assert _event_names(events).count("USB_UNPUBLISHED") == 1


def test_unknown_host_state_defers_and_emits_structured_diagnostic() -> None:
    volume = _volume("D:\\")
    registry = ActivePublicationRegistry()
    calls = []

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (volume,), ()],
        publisher=_stable_published,
        unpublisher=lambda **kwargs: calls.append(kwargs),
        publication_registry=registry,
        host_process_probe=FakeHostProcessProbe(
            [OSError("process inventory unavailable")]
        ),
        cycles=2,
    )

    assert calls == []
    assert len(registry.snapshot()) == 1
    unknown = next(
        item for item in events if item.event == "HOST_PROCESS_STATE_UNKNOWN"
    )
    assert unknown.details["context"] == "USB_REMOVAL"
    assert unknown.details["error"] == "OSError"
    deferred = next(
        item for item in events if item.event == "USB_UNPUBLICATION_DEFERRED"
    )
    assert deferred.details["reason"] == "HOST_STATE_UNKNOWN"


def test_legacy_path_failure_defers_and_reports_the_query_diagnostic() -> None:
    volume = _volume("D:\\")
    registry = ActivePublicationRegistry()
    calls = []
    probe = WindowsTraktorProcessProbe(
        lambda: (WindowsProcessEntry(42, "Traktor.exe"),),
        lambda _pid: (_ for _ in ()).throw(PermissionError("access denied")),
    )

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (volume,), ()],
        publisher=_stable_published,
        unpublisher=lambda **kwargs: calls.append(kwargs),
        publication_registry=registry,
        host_process_probe=probe,
        cycles=2,
    )

    assert calls == []
    unknown = next(
        item for item in events if item.event == "HOST_PROCESS_STATE_UNKNOWN"
    )
    assert unknown.details["context"] == "USB_REMOVAL"
    assert unknown.details["stage"] == "QUERY_FULL_PROCESS_IMAGE_NAME"
    assert unknown.details["pid"] == 42


def test_duplicate_absent_snapshots_create_one_logical_deferred_state() -> None:
    volume = _volume("D:\\")
    registry = ActivePublicationRegistry()

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (volume,), (), (), ()],
        publisher=_stable_published,
        unpublisher=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        publication_registry=registry,
        host_process_probe=FakeHostProcessProbe(
            [HostProcessState.RUNNING]
        ),
        cycles=4,
    )

    assert len(registry.snapshot()) == 1
    assert _event_names(events).count("USB_UNPUBLICATION_DEFERRED") == 1


def test_two_running_host_deferrals_remain_independent() -> None:
    first = _volume("D:\\")
    second = _volume("F:\\")
    registry = ActivePublicationRegistry()
    calls = []

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (first, second), ()],
        publisher=_stable_published,
        unpublisher=lambda **kwargs: calls.append(kwargs),
        publication_registry=registry,
        host_process_probe=FakeHostProcessProbe(
            [HostProcessState.RUNNING]
        ),
        cycles=2,
    )

    assert calls == []
    assert len(registry.snapshot()) == 2
    deferred = [
        item for item in events if item.event == "USB_UNPUBLICATION_DEFERRED"
    ]
    assert [item.volume for item in deferred] == ["D:\\", "F:\\"]
    assert len({item.details["identity_id"] for item in deferred}) == 2


def test_already_detached_vhdx_completes_idempotently_once() -> None:
    volume = _volume("D:\\")
    calls = []

    def unpublish(*, publication):
        calls.append(publication.vhdx_path)
        return SimpleNamespace(
            changed=False,
            vhdx_mount_before=None,
            vhdx_mount_after=None,
        )

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (volume,), (), ()],
        unpublisher=unpublish,
        cycles=3,
    )

    assert calls == [Path("C:\\managed\\d.vhdx")]
    unpublished = next(item for item in events if item.event == "USB_UNPUBLISHED")
    assert unpublished.details["changed"] is False


def test_managed_vhdx_removal_during_detach_does_not_recurse() -> None:
    usb = _volume("D:\\")
    vhdx = _volume("Q:\\", label="TRAKTORBOX", drive_type=3)
    calls = []

    def unpublish(*, publication):
        calls.append(publication.volume)
        return SimpleNamespace(
            changed=True,
            vhdx_mount_before=Path("Q:\\"),
            vhdx_mount_after=None,
        )

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (usb,), (usb, vhdx), ()],
        unpublisher=unpublish,
        cycles=3,
    )

    assert calls == ["D:\\"]
    removed = [item.volume for item in events if item.event == "VOLUME_REMOVED"]
    assert removed == ["D:\\"]


def test_removed_common_volume_never_unpublishes() -> None:
    volume = _volume("F:\\")
    calls = []

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (volume,), ()],
        statuses={"F:\\": PreflightStatus.UNSUPPORTED},
        unpublisher=lambda *, publication: calls.append(publication),
        cycles=2,
    )

    assert calls == []
    assert any(item.event == "VOLUME_REMOVED" and item.volume == "F:\\" for item in events)


def test_usb_removed_after_failed_publish_has_no_partial_registry_or_unpublish() -> None:
    volume = _volume("D:\\")
    registry = ActivePublicationRegistry()
    unpublish_calls = []

    def publish(_volume: DriveInfo):
        raise RuntimeError("USB disappeared during publish")

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (volume,), ()],
        publisher=publish,
        unpublisher=lambda *, publication: unpublish_calls.append(publication),
        publication_registry=registry,
        cycles=2,
    )

    assert registry.snapshot() == ()
    assert unpublish_calls == []
    assert any(item.event == "VOLUME_ERROR" for item in events)
    assert "WATCH_STOPPED" in _event_names(events)


def test_detach_failure_preserves_registry_and_does_not_stop_other_usb() -> None:
    first = _volume("D:\\")
    second = _volume("F:\\")
    registry = ActivePublicationRegistry()
    calls = []

    def unpublish(*, publication):
        calls.append(publication.volume)
        if publication.volume == "D:\\":
            raise OSError("held by Traktor")
        return SimpleNamespace(
            changed=True,
            vhdx_mount_before=Path("R:\\"),
            vhdx_mount_after=None,
        )

    _result, _observer, _clock, events, _publish_calls = _run(
        [(), (first, second), (second,), ()],
        unpublisher=unpublish,
        publication_registry=registry,
        cycles=3,
    )

    assert calls == ["D:\\", "F:\\"]
    assert [item.volume for item in registry.snapshot()] == ["D:\\"]
    error = next(item for item in events if item.event == "USB_UNPUBLICATION_ERROR")
    assert error.details["error"] == "OSError"
    assert error.details["message"] == "held by Traktor"
    assert any(
        item.event == "USB_UNPUBLISHED" and item.volume == "F:\\" for item in events
    )


def test_removing_one_of_two_published_usbs_keeps_the_other_registered() -> None:
    first = _volume("D:\\")
    second = _volume("F:\\")
    registry = ActivePublicationRegistry()
    calls = []

    def unpublish(*, publication):
        calls.append(publication.volume)
        return SimpleNamespace(
            changed=True,
            vhdx_mount_before=Path("Q:\\"),
            vhdx_mount_after=None,
        )

    _run(
        [(), (first, second), (second,)],
        unpublisher=unpublish,
        publication_registry=registry,
        cycles=2,
    )

    assert calls == ["D:\\"]
    assert [item.volume for item in registry.snapshot()] == ["F:\\"]


def test_publish_existing_registration_is_unpublished_on_later_removal() -> None:
    volume = _volume("D:\\")
    calls = []

    def unpublish(*, publication):
        calls.append(publication.volume)
        return SimpleNamespace(
            changed=True,
            vhdx_mount_before=Path("Q:\\"),
            vhdx_mount_after=None,
        )

    _run(
        [(volume,), (volume,), ()],
        publish_existing=True,
        unpublisher=unpublish,
        cycles=2,
    )

    assert calls == ["D:\\"]


def test_usb_reinsertion_publishes_again_without_managed_vhdx_recursion() -> None:
    usb = _volume("D:\\")
    managed = _volume("Q:\\", label="TRAKTORBOX_RECOVERED", drive_type=3)
    unpublish_calls = []

    def unpublish(*, publication):
        unpublish_calls.append(publication.volume)
        return SimpleNamespace(
            changed=True,
            vhdx_mount_before=Path("Q:\\"),
            vhdx_mount_after=None,
        )

    _result, _observer, _clock, events, publish_calls = _run(
        [(), (usb,), (managed,), (usb, managed)],
        unpublisher=unpublish,
        cycles=3,
    )

    assert publish_calls == ["D:\\", "D:\\"]
    assert unpublish_calls == ["D:\\"]
    assert all(
        item.volume != "Q:\\"
        for item in events
        if item.event in {"USB_PUBLICATION_STARTED", "USB_PUBLISHED"}
    )
