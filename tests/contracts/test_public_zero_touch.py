"""Independent synthetic USBs through the installed public lifecycle composition."""

from dataclasses import replace
from pathlib import Path
import sys

import pytest

from rekordbox_usb.host_process import HostProcessState
from rekordbox_usb.public_cli import main
from rekordbox_usb.public_runtime import PublicRuntime, PublicRuntimeError
from rekordbox_usb.public_watch import PublicWatchRuntime, exclusive_runtime
from rekordbox_usb.usb_watch import WatchUsbConfig
from rekordbox_usb.windows_drives import DriveInfo
from tests.contracts.test_public_executable import _Backend, _fixture


class MultiBackend:
    """Separate mount and backend state for every synthetic VHDX."""
    def __init__(self, root):
        self.root, self.by_path, self.fail = root, {}, set()

    def backend(self, path):
        return self.by_path.setdefault(path, _Backend(self.root / ("mount-" + path.stem)))

    def inspect(self, path):
        return self.backend(path).inspect(path)

    def create_vhdx(self, path, *args):
        if path.stem in self.fail:
            raise RuntimeError("synthetic creation failure")
        self.backend(path).create_vhdx(path, *args)

    def attach(self, path, *args):
        self.backend(path).attach(path, *args)

    def initialize_if_needed(self, path, *args):
        self.backend(path).initialize_if_needed(path, *args)

    def format_if_needed(self, path, *args):
        self.backend(path).format_if_needed(path, *args)

    def assign_mount(self, path, *args):
        backend = self.backend(path)
        if backend.mount.exists():
            backend.state = replace(backend.inspect(path), mount_point=backend.mount)
            return backend.mount
        return backend.assign_mount(path, *args)

    def detach(self, path, *args):
        self.backend(path).detach(path, *args)

    def remove_vhdx(self, path):
        raise AssertionError("must preserve managed catalogs")


class Observer:
    def __init__(self, snapshots):
        self.snapshots, self.current, self.index, self.closed = snapshots, (), 0, False

    def snapshot(self):
        value = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        if isinstance(value, BaseException):
            raise value
        self.current = tuple(value() if callable(value) else value)
        return self.current

    def close(self):
        self.closed = True


class Host:
    current = HostProcessState.NOT_RUNNING

    def state(self):
        return self.current


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def usb(tmp_path, number, **metadata):
    root = tmp_path / f"usb{number}"
    root.mkdir()
    _fixture(root)
    return DriveInfo(root, metadata.get("label", "Same label"), metadata.get("serial", f"SERIAL{number}"),
                     "FAT32", metadata.get("drive_type", 2), metadata.get("bus_type", 7))


def scenario(tmp_path, snapshots, *, host=None, config=None, sink=None, backend=None, **options):
    observer, clock, events = Observer(snapshots), Clock(), []
    backend, host = backend or MultiBackend(tmp_path), host or Host()
    runtime = PublicRuntime(volumes=lambda: observer.current, backend_factory=lambda _: backend,
                            capability_probe=lambda: True, host_probe=host, require_windows=False)
    def emit(event):
        events.append(event)
        if sink:
            sink(event)
    watch = PublicWatchRuntime(runtime, tmp_path / "data", observer=observer,
                               config=config or WatchUsbConfig(publish_existing=True, debounce_seconds=0,
                                                                retry_seconds=0.1, poll_interval_seconds=0.1),
                               event_sink=emit, clock=clock, sleep=clock.sleep, **options)
    return watch, observer, backend, events


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_startup_all_independent_sources(tmp_path, count):
    volumes = tuple(usb(tmp_path, number) for number in range(count))
    watch, observer, backend, events = scenario(tmp_path, [volumes, volumes, volumes])
    assert watch.run(max_cycles=2) == 0
    active = watch.watcher.active_publications
    assert len(active) == count
    for attribute in ("identity_id", "source_id", "vhdx_path", "vhdx_mount"):
        assert len({getattr(item, attribute) for item in active}) == count
    assert sum(event.event == "USB_PUBLISHED" for event in events) == count
    assert all(state["state"] == "PUBLISHED" for state in watch.sources.values())
    for item in active:
        assert backend.backend(item.vhdx_path).calls.count("create") == 1
        assert list(item.vhdx_mount.rglob("*.nml"))
        assert not list(item.vhdx_mount.rglob("*.mp3"))
    assert observer.closed
    if not count:
        assert not (tmp_path / "data").exists()


def test_hotplug_second_usb_and_remove_only_second(tmp_path):
    a, b = usb(tmp_path, 1), usb(tmp_path, 2)
    watch, _, backend, events = scenario(tmp_path, [(a,), (a,), (a, b), (a, b), (a,)])
    assert watch.run(max_cycles=4) == 0
    assert len(watch.watcher.active_publications) == 1
    first = watch.watcher.active_publications[0]
    assert first.volume == str(a.root)
    assert "detach" not in backend.backend(first.vhdx_path).calls
    assert [event.volume for event in events if event.event == "USB_UNPUBLISHED"] == [str(b.root)]
    assert sorted(state["state"] for state in watch.sources.values()) == ["PUBLISHED", "REMOVED"]


def test_arrival_after_zero_usb(tmp_path):
    a = usb(tmp_path, 1)
    watch, _, _, events = scenario(tmp_path, [(), (), (a,), (a,)])
    assert watch.run(max_cycles=3) == 0
    assert len(watch.watcher.active_publications) == 1
    assert sum(event.event == "USB_PUBLISHED" for event in events) == 1


@pytest.mark.parametrize("different_letter", [False, True])
def test_reconnection_reuses_identity_and_vhdx(tmp_path, different_letter):
    a = usb(tmp_path, 1)
    def returned():
        if different_letter:
            target = tmp_path / "new-letter"
            a.root.rename(target)
            return (replace(a, root=target),)
        return (a,)
    watch, _, backend, events = scenario(tmp_path, [(a,), (a,), (), returned])
    assert watch.run(max_cycles=3) == 0
    active = watch.watcher.active_publications
    assert len(active) == 1
    publications = [event for event in events if event.event == "USB_PUBLISHED"]
    assert len(publications) == 2
    assert len({event.details["source_id"] for event in publications}) == 1
    assert backend.backend(active[0].vhdx_path).calls.count("create") == 1
    assert len(list((tmp_path / "data/managed").glob("*.vhdx"))) == 1


@pytest.mark.parametrize("failure", ["preflight", "publish"])
def test_source_failure_does_not_block_other(tmp_path, failure):
    a, b = usb(tmp_path, 1), usb(tmp_path, 2)
    backend = MultiBackend(tmp_path)
    if failure == "preflight":
        (a.root / "PIONEER/rekordbox/export.pdb").write_bytes(b"bad synthetic library")
    else:
        backend.fail.add(PublicRuntime._source_id(a.serial)[0])
    watch, _, _, events = scenario(tmp_path, [(a, b), (a, b)], backend=backend)
    assert watch.run(max_cycles=1) == 0
    assert [item.volume for item in watch.watcher.active_publications] == [str(b.root)]
    assert any(event.event in ("VOLUME_ERROR", "VOLUME_IGNORED") for event in events)


def test_managed_internal_and_fixed_usb(tmp_path):
    managed = usb(tmp_path, 1, drive_type=3, bus_type=15)
    local = usb(tmp_path, 2, drive_type=3, bus_type=11)
    fixed_usb = usb(tmp_path, 3, drive_type=3, bus_type=7)
    watch, _, _, _ = scenario(tmp_path, [(managed, local, fixed_usb)] * 2)
    assert watch.run(max_cycles=1) == 0
    assert [item.volume for item in watch.watcher.active_publications] == [str(fixed_usb.root)]


def test_serial_collision_is_blocked_before_second_publication(tmp_path):
    a, b = usb(tmp_path, 1), usb(tmp_path, 2, serial="SERIAL1")
    watch, _, backend, events = scenario(tmp_path, [(a, b)] * 2)
    assert watch.run(max_cycles=1) == 0
    assert len(watch.watcher.active_publications) == len(backend.by_path) == 0
    assert any("AMBIGUOUS_SOURCE_IDENTITY" in event.details.get("reasons", ()) for event in events)


def test_removal_during_debounce_never_publishes(tmp_path):
    a = usb(tmp_path, 1)
    config = WatchUsbConfig(publish_existing=True, debounce_seconds=1, retry_seconds=1,
                            poll_interval_seconds=0.1)
    watch, _, backend, events = scenario(tmp_path, [(), (a,), ()], config=config)
    assert watch.run(max_cycles=2) == 0
    assert not backend.by_path
    assert not watch.watcher.active_publications


@pytest.mark.parametrize("state", [HostProcessState.RUNNING, HostProcessState.UNKNOWN])
def test_removal_and_shutdown_defer_while_host_protected(tmp_path, state):
    a, b, host = usb(tmp_path, 1), usb(tmp_path, 2), Host()
    def protect():
        host.current = state
        return (a,)
    watch, _, backend, events = scenario(tmp_path, [(a, b), (a, b), protect, KeyboardInterrupt()], host=host)
    code = watch.run()
    assert code == (3 if state is HostProcessState.UNKNOWN else 0)
    assert len(watch.watcher.active_publications) == 2
    assert len(watch.watcher.deferred_unpublications) == 1
    assert not any("detach" in item.calls for item in backend.by_path.values())
    assert any(event.event == "USB_UNPUBLICATION_DEFERRED" for event in events)


def test_ctrl_c_cleans_all_sources(tmp_path):
    a, b = usb(tmp_path, 1), usb(tmp_path, 2)
    watch, observer, backend, events = scenario(tmp_path, [(a, b), (a, b), KeyboardInterrupt()])
    assert watch.run() == 0
    assert observer.closed and not watch.watcher.active_publications
    assert all(item.calls.count("detach") == 1 for item in backend.by_path.values())
    assert sum(event.event == "SHUTDOWN_UNPUBLISHED" for event in events) == 2


def test_multiple_vhdx_startup_recovery_then_republish(tmp_path):
    a, b = usb(tmp_path, 1), usb(tmp_path, 2)
    first, _, backend, _ = scenario(tmp_path, [(a, b)] * 2)
    assert first.run(max_cycles=1) == 0
    second, _, _, events = scenario(tmp_path, [(a, b)] * 2, backend=backend)
    assert second.run(max_cycles=1) == 0
    assert len(second.watcher.active_publications) == 2
    assert sum(event.event == "ORPHANED_VHDX_UNPUBLISHED" for event in events) == 2
    assert all(item.calls.count("create") == 1 for item in backend.by_path.values())


def test_startup_orphans_cleanup_independently(tmp_path):
    a, b = usb(tmp_path, 1), usb(tmp_path, 2)
    first, _, backend, _ = scenario(tmp_path, [(a, b)] * 2)
    first.run(max_cycles=1)
    second, _, _, events = scenario(tmp_path, [(), ()], backend=backend)
    assert second.run(max_cycles=1) == 0
    assert not second.watcher.active_publications
    assert sum(event.event == "ORPHANED_VHDX_UNPUBLISHED" for event in events) == 2


def test_existing_publications_reconcile_without_writes_when_host_running(tmp_path):
    a, b = usb(tmp_path, 1), usb(tmp_path, 2)
    first, _, backend, _ = scenario(tmp_path, [(a, b)] * 2)
    first.run(max_cycles=1)
    before = {path: tuple(item.calls) for path, item in backend.by_path.items()}
    host = Host()
    host.current = HostProcessState.RUNNING
    second, _, _, events = scenario(tmp_path, [(a, b)] * 2, backend=backend, host=host)
    assert second.run(max_cycles=1) == 0
    assert len(second.watcher.active_publications) == 2
    assert before == {path: tuple(item.calls) for path, item in backend.by_path.items()}


def test_hot_insert_new_source_allowed_while_host_running(tmp_path):
    a, host = usb(tmp_path, 1), Host()
    host.current = HostProcessState.RUNNING
    watch, _, _, _ = scenario(tmp_path, [(), (a,)], host=host)
    assert watch.run(max_cycles=1) == 0
    assert len(watch.watcher.active_publications) == 1


def test_start_with_running_host_defers_detached_cache_then_retries_safely(tmp_path):
    from rekordbox_usb.public_launcher import WorkerJournal
    a, host = usb(tmp_path, 1), Host()
    first, _, backend, _ = scenario(tmp_path, [(a,)], host=host)
    first.run(max_cycles=1)
    active = first.watcher.active_publications[0]
    first.runtime.detach_managed(active.source_id, active.vhdx_path, first.data_root)
    before = {path: tuple(item.calls) for path, item in backend.by_path.items()}
    host.current = HostProcessState.RUNNING
    journal_root = tmp_path / "journal"
    journal_root.mkdir()
    journal = WorkerJournal(journal_root)
    def host_closed():
        assert before == {path: tuple(item.calls) for path, item in backend.by_path.items()}
        host.current = HostProcessState.NOT_RUNNING
        return (a,)
    second, _, _, events = scenario(tmp_path, [(a,), (a,), host_closed], backend=backend, host=host,
                                    sink=journal.event, on_startup_complete=journal.ready)
    assert second.run(max_cycles=2) == 0
    assert any(event.event == "USB_PUBLICATION_DEFERRED" for event in events)
    assert len(second.watcher.active_publications) == 1


def test_inventory_failure_does_not_remove_published_sources(tmp_path):
    a = usb(tmp_path, 1)
    watch, _, backend, events = scenario(tmp_path, [(a,), (a,), OSError("synthetic inventory unavailable"), (a,)])
    assert watch.run(max_cycles=3) == 0
    assert len(watch.watcher.active_publications) == 1
    assert all("detach" not in item.calls for item in backend.by_path.values())
    assert any(event.event == "WATCH_ERROR" for event in events)


def test_no_argument_cli_defaults_and_continues_until_interrupt(tmp_path, capsys, monkeypatch):
    watch, observer, _, events = scenario(tmp_path, [(), (), KeyboardInterrupt()])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    def factory(runtime, data_root, *, event_sink):
        assert data_root == tmp_path / "RekordboxUSB"
        watch._sink = event_sink
        return watch
    assert main([], runtime=watch.runtime, watch_factory=factory) == 0
    assert observer.index == 3 and observer.closed
    assert "WATCH_STARTED" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named runtime guard")
def test_second_continuous_runtime_rejected_without_disk_changes(tmp_path):
    with exclusive_runtime(tmp_path / "data"):
        with pytest.raises(PublicRuntimeError, match="RUNTIME_ALREADY_RUNNING"):
            with exclusive_runtime(tmp_path / "data"):
                pytest.fail("second storage manager admitted")
    assert not (tmp_path / "data").exists()


def test_not_ready_volume_retries_without_replug(tmp_path):
    a = usb(tmp_path, 1)
    class Readiness:
        calls = 0
        def is_accessible(self, root):
            self.calls += 1
            return self.calls > 5
    readiness = Readiness()
    watch, _, _, _ = scenario(tmp_path, [(a,)] * 4, readiness_probe=readiness)
    assert watch.run(max_cycles=3) == 0
    assert readiness.calls >= 6 and len(watch.watcher.active_publications) == 1


def test_transient_publish_failure_retries_independently(tmp_path):
    a, b = usb(tmp_path, 1), usb(tmp_path, 2)
    backend = MultiBackend(tmp_path)
    identity = PublicRuntime._source_id(a.serial)[0]
    backend.fail.add(identity)
    def repair():
        backend.fail.clear()
        return (a, b)
    watch, _, _, _ = scenario(tmp_path, [(a, b), (a, b), repair], backend=backend)
    assert watch.run(max_cycles=2) == 0
    assert len(watch.watcher.active_publications) == 2


def test_deferred_reconnection_new_letter_waits_for_host_clear(tmp_path):
    a, b, host = usb(tmp_path, 1), usb(tmp_path, 2), Host()
    moved = replace(b, root=tmp_path / "new-letter")
    def protect():
        host.current = HostProcessState.RUNNING
        return (a,)
    def reconnect():
        b.root.rename(moved.root)
        return (a, moved)
    def host_closed():
        host.current = HostProcessState.NOT_RUNNING
        return (a, moved)
    watch, _, backend, events = scenario(tmp_path, [(a, b), (a, b), protect, reconnect, host_closed], host=host)
    assert watch.run(max_cycles=4) == 0
    active = watch.watcher.active_publications
    assert len(active) == 2
    assert {item.volume for item in active} == {str(a.root), str(moved.root)}
    first_path = watch.runtime._paths(watch.data_root, watch.runtime._source_id(a.serial)[0])[1]
    assert "detach" not in backend.backend(first_path).calls
    assert any(event.event == "USB_SOURCE_RELOCATION_DEFERRED" for event in events)


def test_letter_replaced_without_empty_snapshot(tmp_path):
    a, b = usb(tmp_path, 1), usb(tmp_path, 2)
    replacement = replace(a, serial="NEW_SERIAL")
    watch, _, backend, _ = scenario(tmp_path, [(a, b), (a, b), (replacement, b)])
    assert watch.run(max_cycles=2) == 0
    active = watch.watcher.active_publications
    assert len(active) == 2
    assert watch.runtime._source_id(replacement.serial)[1] in {item.source_id for item in active}
    b_path = watch.runtime._paths(watch.data_root, watch.runtime._source_id(b.serial)[0])[1]
    assert "detach" not in backend.backend(b_path).calls


def test_source_identity_change_after_preflight_blocks_effects(tmp_path):
    a = usb(tmp_path, 1)
    watch, observer, backend, _ = scenario(tmp_path, [(a,)])
    observer.current = (a,)
    watch._preflight(volume_root=a.root)
    observer.current = (replace(a, serial="REPLACED"),)
    with pytest.raises(PublicRuntimeError, match="SOURCE_IDENTITY_CHANGED"):
        watch._publish(volume_root=a.root)
    assert not backend.by_path and not watch.data_root.exists()


def test_all_sources_remain_read_only_through_lifecycle(tmp_path):
    a, b = usb(tmp_path, 1), usb(tmp_path, 2)
    before = {path: path.read_bytes() for volume in (a, b) for path in volume.root.rglob("*") if path.is_file()}
    watch, _, _, _ = scenario(tmp_path, [(a, b), (a, b), (), KeyboardInterrupt()])
    assert watch.run() == 0
    after = {path: path.read_bytes() for volume in (a, b) for path in volume.root.rglob("*") if path.is_file()}
    assert before == after


def test_disk_operations_are_serialized_across_runtime_instances(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    a, b = usb(tmp_path, 1), usb(tmp_path, 2)
    lock = threading.Lock()
    class Backend(MultiBackend):
        active = 0
        maximum = 0
        def create_vhdx(self, path, *args):
            with lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            try:
                time.sleep(0.03)
                return super().create_vhdx(path, *args)
            finally:
                with lock:
                    self.active -= 1
    backend = Backend(tmp_path)
    runtimes = [PublicRuntime(volumes=lambda: (a, b), backend_factory=lambda _: backend,
                              capability_probe=lambda: True, host_probe=Host(), require_windows=False)
                for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(runtime.publish, volume.root, tmp_path / "data")
                for runtime, volume in zip(runtimes, (a, b))]
        assert len({job.result().source_id for job in jobs}) == 2
    assert backend.maximum == 1
