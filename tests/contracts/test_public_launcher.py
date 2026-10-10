"""Installer discovery and the storage-before-host/cleanup-after-host contract."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from rekordbox_usb.host_process import HostProcessState
from rekordbox_usb.public_launcher import (
    LauncherError, TraktorInstallation, WorkerLifetime, discover_installations,
    host_probe, launch, preflight_sources, service, shortcut_records, supervise,
    supervise_service, WorkerJournal, write_status,
)
from tests.contracts.test_public_zero_touch import Host, scenario, usb


@pytest.mark.parametrize("versions", [(), ("2",), ("3",), ("4",), ("2", "3", "4")])
def test_one_service_shortcut_uses_latest_installed_icon(tmp_path, versions):
    for version in versions:
        directory = tmp_path / "Program Files" / "Native Instruments" / f"Traktor Pro {version}"
        directory.mkdir(parents=True)
        (directory / ("Traktor Pro 4.exe" if version == "4" else "Traktor.exe")).touch()
    # Unrelated backup must never become an installation.
    (tmp_path / "backup").mkdir()
    (tmp_path / "backup" / "Traktor.exe").touch()
    installed = discover_installations(program_roots=[tmp_path / "Program Files"], registry_dirs={})
    assert tuple(item.version for item in installed) == versions
    records = shortcut_records(installed, tmp_path / "Python Space" / "pythonw.exe", tmp_path / "data")
    assert len(records) == 1
    record = records[0]
    assert record["name"] == "TRAKFROM-BX.lnk"
    assert "public_launcher service" in record["arguments"]
    assert "--traktor-version" not in record["arguments"]
    assert record["icon"] == str(installed[-1].executable if installed else
                                  tmp_path / "Python Space" / "pythonw.exe") + ",0"
    assert record["target"].endswith("pythonw.exe")
    assert record["working_directory"] == str(tmp_path / "Python Space")


def test_registry_custom_directory_has_priority_and_must_contain_host(tmp_path):
    directory = tmp_path / "DJ Synthetic" / "Traktor"
    directory.mkdir(parents=True)
    (directory / "Traktor.exe").touch()
    installed = discover_installations(program_roots=[], registry_dirs={
        "2": [tmp_path / "missing", directory], "4": [directory]})
    assert installed == (TraktorInstallation("2", (directory / "Traktor.exe").resolve()),)


def test_custom_install_is_added_to_conservative_host_catalog():
    installed = [TraktorInstallation("3", Path(r"C:\DJ Suite\Traktor.exe"))]
    probe = host_probe(installed)
    from rekordbox_usb.host_process import WindowsProcessEntry
    probe._enumerate_processes = lambda: [WindowsProcessEntry(12, "Traktor.exe")]
    probe._query_process_image_path = lambda _: r"C:\DJ Suite\Traktor.exe"
    assert probe.state() is HostProcessState.RUNNING
    probe._query_process_image_path = lambda _: r"C:\other\Traktor.exe"
    assert probe.state() is HostProcessState.NOT_RUNNING


@pytest.mark.parametrize("code", ["READY", "UNSUPPORTED_LIBRARY", "INVALID_LIBRARY"])
def test_read_only_preflight_blocks_bad_sources_before_elevation(tmp_path, code):
    runtime = SimpleNamespace(_volumes=lambda: [SimpleNamespace(root=tmp_path)],
                              eligible_volume=lambda *_: True,
                              preflight=lambda *_: SimpleNamespace(ready=code == "READY", code=code))
    if code == "INVALID_LIBRARY":
        with pytest.raises(LauncherError, match=code):
            preflight_sources(runtime, tmp_path)
    else:
        preflight_sources(runtime, tmp_path)


class Clock:
    now = 0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def test_host_opens_after_ready_and_worker_ends_after_stop_request(tmp_path):
    clock, order, states = Clock(), [], []
    def worker_poll():
        if (tmp_path / "stop.request").exists():
            order.append("cleanup")
            return 0
        if clock.now >= 1:
            write_status(tmp_path, "READY")
        return None
    def start_host():
        assert (tmp_path / "status.json").exists()
        order.append("host")
        return SimpleNamespace(poll=lambda: 0 if clock.now >= 2 else None)
    worker = SimpleNamespace(poll=worker_poll)
    assert supervise(worker, tmp_path, start_host, clock=clock, sleep=clock.sleep,
                     on_state=states.append) == 0
    assert order[0] == "host" and order[-1] == "cleanup"
    assert clock.now >= 2
    assert states == ["Preparando volumes", "Abrindo Traktor", "Ativo", "Encerrando"]


@pytest.mark.parametrize("failure", [None, "preflight", "elevation", "tray", "host"])
def test_tray_follows_launch_lifetime_and_always_disappears(tmp_path, monkeypatch, failure):
    import ctypes
    import rekordbox_usb.public_launcher as launcher
    order = []
    class Tray:
        def __init__(self, *_):
            pass
        def start(self):
            order.append("tray_started")
            if failure == "tray":
                raise LauncherError("TRAY_UNAVAILABLE")
        def update(self, state):
            order.append(state)
        def close(self):
            order.append("tray_closed")
    def preflight(*_):
        order.append("preflight")
        if failure == "preflight":
            raise LauncherError("INVALID_LIBRARY")
    def elevate(session, _):
        order.append("elevation")
        if failure == "elevation":
            raise LauncherError("STORAGE_ELEVATION_CANCELLED")
        write_status(session, "READY")
        return SimpleNamespace(poll=lambda: 0 if (session / "stop.request").exists() else None,
                               close=lambda: order.append("worker_handle_closed"))
    def start_host(*_, **__):
        order.append("host")
        if failure == "host":
            raise OSError("synthetic host launch failure")
        return SimpleNamespace(poll=lambda: 0)
    monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: False)
    monkeypatch.setattr(launcher, "validate_root", lambda _: None)
    monkeypatch.setattr(launcher, "host_probe", lambda _: SimpleNamespace(
        state=lambda: HostProcessState.NOT_RUNNING))
    monkeypatch.setattr(launcher, "preflight_sources", preflight)
    monkeypatch.setattr(launcher, "elevate_worker", elevate)
    monkeypatch.setattr(launcher.subprocess, "Popen", start_host)
    installation = TraktorInstallation("4", tmp_path / "Traktor Pro 4.exe")
    if failure:
        with pytest.raises((LauncherError, OSError)):
            launch(installation, [installation], tmp_path, tray_factory=Tray)
    else:
        assert launch(installation, [installation], tmp_path, tray_factory=Tray) == 0
        assert order.index("Ativo") < order.index("Encerrando") < order.index("worker_handle_closed")
    assert order[0] == "tray_started" and order[-1] == "tray_closed"
    if failure in ("tray", "preflight"):
        assert "elevation" not in order and "host" not in order


@pytest.mark.parametrize("case,code", [
    ("dead", "STORAGE_STARTUP_FAILED"), ("error", "PREFLIGHT_FAILED"),
    ("timeout", "STORAGE_STARTUP_TIMEOUT"),
])
def test_storage_failure_never_starts_traktor(tmp_path, case, code):
    clock, started = Clock(), []
    if case == "error":
        write_status(tmp_path, "ERROR", code="PREFLIGHT_FAILED")
    worker = SimpleNamespace(poll=lambda: 3 if case == "dead" else None)
    with pytest.raises(LauncherError, match=code):
        supervise(worker, tmp_path, lambda: started.append(True), clock=clock, sleep=clock.sleep,
                  startup_timeout=1)
    assert not started
    assert (tmp_path / "stop.request").exists()


@pytest.mark.parametrize("worker_exit,code", [(3, "STORAGE_CLEANUP_FAILED"),
                                               (None, "STORAGE_CLEANUP_PENDING")])
def test_cleanup_failure_reported_without_forcing_termination(tmp_path, worker_exit, code):
    clock = Clock()
    write_status(tmp_path, "READY")
    worker = SimpleNamespace(poll=lambda: worker_exit if (tmp_path / "stop.request").exists() else None)
    with pytest.raises(LauncherError, match=code):
        supervise(worker, tmp_path, lambda: SimpleNamespace(poll=lambda: 0),
                  clock=clock, sleep=clock.sleep, shutdown_timeout=1)


@pytest.mark.parametrize("trigger", ["parent", "request", "seen"])
@pytest.mark.parametrize("state", list(HostProcessState))
def test_cleanup_waits_for_all_hosts_and_preserves_unknown(tmp_path, trigger, state):
    probe = SimpleNamespace(state=lambda: state)
    parent = SimpleNamespace(poll=lambda: 0 if trigger == "parent" else None)
    stop = tmp_path / "stop.request"
    if trigger == "request":
        stop.touch()
    lifetime = WorkerLifetime(probe, parent, stop, close_grace_seconds=0)
    lifetime.host_seen = trigger == "seen"
    assert lifetime.should_stop() is (state is HostProcessState.NOT_RUNNING)


def test_worker_waits_for_host_before_any_stop_request(tmp_path):
    lifetime = WorkerLifetime(SimpleNamespace(state=lambda: HostProcessState.NOT_RUNNING),
                              SimpleNamespace(poll=lambda: None), tmp_path / "stop.request")
    assert not lifetime.should_stop()


def test_host_close_grace_resets_on_unknown_or_restarted_host(tmp_path):
    clock, host = Clock(), Host()
    lifetime = WorkerLifetime(host, SimpleNamespace(poll=lambda: None), tmp_path / "stop.request",
                              clock=clock, close_grace_seconds=3)
    host.current = HostProcessState.RUNNING
    assert not lifetime.should_stop()
    host.current = HostProcessState.NOT_RUNNING
    clock.now = 1
    assert not lifetime.should_stop()
    clock.now = 3
    assert not lifetime.should_stop()
    host.current = HostProcessState.UNKNOWN
    assert not lifetime.should_stop()
    host.current = HostProcessState.NOT_RUNNING
    clock.now = 4
    assert not lifetime.should_stop()
    clock.now = 6
    assert not lifetime.should_stop()
    clock.now = 7
    assert lifetime.should_stop()


class ServiceUI:
    def __init__(self, *_args, **kwargs):
        self.events, self.cancelled = [], False
        self.startup = kwargs.get("startup")
    def start(self):
        self.events.append("popup")
    def update(self, state):
        self.events.append(state)
    def mark_ready(self):
        self.events.append("tray_ready")
    def stop_requested(self):
        return self.cancelled
    def close(self):
        self.events.append("closed")


def test_service_waits_ready_then_observes_independently_opened_host(tmp_path, monkeypatch):
    import rekordbox_usb.public_launcher as launcher
    clock, ui = Clock(), ServiceUI()
    def poll():
        if clock.now >= 1 and not (tmp_path / "status.json").exists():
            write_status(tmp_path, "READY")
        if clock.now >= 6:
            write_status(tmp_path, "STOPPED", code="CLEANUP_COMPLETE")
            return 0
        return None
    def state():
        return HostProcessState.RUNNING if 3 <= clock.now < 5 else HostProcessState.NOT_RUNNING
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *_args, **_kwargs:
                        pytest.fail("service must never launch Traktor"))
    assert supervise_service(SimpleNamespace(poll=poll), tmp_path, ui,
                             SimpleNamespace(state=state), clock=clock, sleep=clock.sleep) == 0
    assert ui.events[0] == "tray_ready"
    assert ui.events.index("tray_ready") < ui.events.index("Ativo") < ui.events.index("Encerrando volumes")
    assert clock.now == 6


@pytest.mark.parametrize("case,expected", [("failure", "STORAGE_CLEANUP_FAILED"),
                                           ("timeout", "STORAGE_STARTUP_TIMEOUT")])
def test_service_does_not_authorize_host_before_ready(tmp_path, case, expected):
    clock, ui = Clock(), ServiceUI()
    if case == "failure":
        write_status(tmp_path, "ERROR", code="STORAGE_CLEANUP_FAILED")
    with pytest.raises(LauncherError, match=expected):
        supervise_service(SimpleNamespace(poll=lambda: 3 if case == "failure" else None),
                          tmp_path, ui, SimpleNamespace(state=lambda: HostProcessState.NOT_RUNNING),
                          clock=clock, sleep=clock.sleep, startup_timeout=1)
    assert "tray_ready" not in ui.events
    assert (tmp_path / "stop.request").exists()


def test_cancel_popup_before_elevation_never_starts_worker(tmp_path, monkeypatch):
    import ctypes
    import rekordbox_usb.public_launcher as launcher
    ui = ServiceUI(startup=True)
    def preflight(*_):
        assert ui.events == ["popup"]
        ui.cancelled = True
    monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: False)
    monkeypatch.setattr(launcher, "validate_root", lambda _: None)
    monkeypatch.setattr(launcher, "host_probe", lambda _: SimpleNamespace(
        state=lambda: HostProcessState.NOT_RUNNING))
    monkeypatch.setattr(launcher, "preflight_sources", preflight)
    monkeypatch.setattr(launcher, "elevate_worker", lambda *_: pytest.fail("cancelled popup elevated worker"))
    assert service([], tmp_path, ui_factory=lambda *_args, **_kwargs: ui) == 0
    assert ui.events == ["popup", "closed"]


@pytest.mark.parametrize("state", list(HostProcessState))
def test_service_can_start_with_running_host_but_rejects_unknown(tmp_path, monkeypatch, state):
    import ctypes
    import rekordbox_usb.public_launcher as launcher
    ui, calls = ServiceUI(startup=True), []
    worker = SimpleNamespace(close=lambda: calls.append("closed"))
    monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: False)
    monkeypatch.setattr(launcher, "validate_root", lambda _: None)
    monkeypatch.setattr(launcher, "host_probe", lambda _: SimpleNamespace(state=lambda: state))
    monkeypatch.setattr(launcher, "preflight_sources", lambda *_: calls.append("preflight"))
    monkeypatch.setattr(launcher, "elevate_worker", lambda *_: (calls.append("elevate") or worker))
    monkeypatch.setattr(launcher, "supervise_service", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *_args, **_kwargs:
                        pytest.fail("service must not open a second Traktor"))
    if state is HostProcessState.UNKNOWN:
        with pytest.raises(LauncherError, match="HOST_STATE_UNKNOWN"):
            service([], tmp_path, ui_factory=lambda *_args, **_kwargs: ui)
        assert not calls and not ui.events
    else:
        assert service([], tmp_path, ui_factory=lambda *_args, **_kwargs: ui) == 0
        assert calls == ["preflight", "elevate", "closed"]
        assert ui.events == ["popup", "Aguardando autorização do Windows", "closed"]


def test_deferred_catalog_is_ready_without_false_startup_failure(tmp_path):
    from rekordbox_usb.usb_watch import WatchUsbEvent
    import json
    journal = WorkerJournal(tmp_path)
    journal.event(WatchUsbEvent("USB_PUBLICATION_DEFERRED", None, {
        "source_id": "synthetic-source", "reason": "TRAKTOR_RUNNING"}))
    journal.ready()
    assert json.loads((tmp_path / "status.json").read_text())["deferred_publications"] == 1
    journal.event(WatchUsbEvent("USB_PUBLISHED", None, {"source_id": "synthetic-source"}))
    assert json.loads((tmp_path / "status.json").read_text())["deferred_publications"] == 0


def test_transient_runtime_warning_does_not_report_failed_cleanup(tmp_path):
    from rekordbox_usb.usb_watch import WatchUsbEvent
    journal = WorkerJournal(tmp_path)
    journal.ready()
    journal.event(WatchUsbEvent("WATCH_ERROR", None, {"diagnostic": {
        "code": "OPERATION_FAILURE", "message": "private message", "path": "private path"}}))
    journal.event(WatchUsbEvent("SHUTDOWN_CLEANUP_STARTED", None, {}))
    journal.event(WatchUsbEvent("SHUTDOWN_CLEANUP_COMPLETED", None, {
        "cleaned": 2, "already_detached": 0, "errors": 0, "deferred": 0}))
    assert journal.finish(0, 0) == 0
    import json
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "STOPPED" and status["runtime_warnings"] == 1
    assert "private" not in (tmp_path / "events.jsonl").read_text()


def test_failed_owned_volume_recovery_never_authorizes_host_start(tmp_path):
    from rekordbox_usb.usb_watch import WatchUsbEvent
    journal = WorkerJournal(tmp_path)
    journal.event(WatchUsbEvent("STARTUP_RECOVERY_ERROR", None, {"diagnostic_code": "OWNERSHIP_FAILURE"}))
    with pytest.raises(LauncherError, match="OWNERSHIP_FAILURE"):
        journal.ready()
    assert not (tmp_path / "status.json").exists()


@pytest.mark.parametrize("result,errors,deferred,remaining", [(3, 1, 0, 1), (0, 0, 1, 1), (0, 0, 0, 1)])
def test_real_cleanup_error_or_remaining_volume_is_never_reported_success(tmp_path, result, errors, deferred, remaining):
    from rekordbox_usb.usb_watch import WatchUsbEvent
    journal = WorkerJournal(tmp_path)
    journal.ready()
    journal.event(WatchUsbEvent("SHUTDOWN_CLEANUP_COMPLETED", None, {
        "cleaned": 0, "already_detached": 0, "errors": errors, "deferred": deferred}))
    assert journal.finish(result, remaining) == 3
    assert '"state": "ERROR"' in (tmp_path / "status.json").read_text()


@pytest.mark.parametrize("count", [0, 1, 2])
def test_supervised_stop_detaches_all_owned_sources(tmp_path, count):
    volumes = tuple(usb(tmp_path, number) for number in range(count))
    ready, host = [], Host()
    watch, observer, backend, events = scenario(
        tmp_path, [volumes], host=host, stop_requested=lambda: bool(ready),
        on_startup_complete=lambda: ready.append(True))
    assert watch.run() == 0
    assert ready and observer.closed
    assert not watch.watcher.active_publications
    assert len([event for event in events if event.event == "SHUTDOWN_UNPUBLISHED"]) == count
    assert all(not item.state.attached for item in backend.by_path.values())


def test_failure_between_storage_and_host_cleans_owned_sources(tmp_path):
    volumes = (usb(tmp_path, 1), usb(tmp_path, 2))
    def fail():
        raise LauncherError("SYNTHETIC_STARTUP_FAILURE")
    watch, observer, backend, events = scenario(
        tmp_path, [volumes], stop_requested=lambda: False, on_startup_complete=fail)
    with pytest.raises(LauncherError, match="SYNTHETIC_STARTUP_FAILURE"):
        watch.run()
    assert observer.closed
    assert not watch.watcher.active_publications
    assert all(not item.state.attached for item in backend.by_path.values())
