"""Preference persistence and live lifecycle choices, with synthetic state only."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rekordbox_usb.desktop_preferences import DesktopPreferences, DesktopSettings, PreferenceStore
from rekordbox_usb.host_process import HostProcessState
from rekordbox_usb.public_launcher import WorkerLifetime
from rekordbox_usb.windows_startup import WindowsStartup
from rekordbox_usb.windows_tray import WindowsTray, _NativeTray


class Startup:
    value = False
    def enabled(self):
        return self.value
    def set_enabled(self, enabled):
        self.value = enabled


def test_preferences_persist_and_platform_state_is_authoritative(tmp_path):
    startup = Startup()
    settings = DesktopSettings(PreferenceStore(tmp_path), startup)
    assert settings.current() == DesktopPreferences()
    settings.toggle("run_on_startup")
    settings.toggle("shutdown_when_traktor_closes")
    assert PreferenceStore(tmp_path).load() == DesktopPreferences(True, False)
    assert DesktopSettings(PreferenceStore(tmp_path), startup).current() == DesktopPreferences(True, False)
    startup.value = False  # Removal outside the app is reflected by the menu.
    assert not settings.current().run_on_startup
    settings.toggle("run_on_startup")
    settings.toggle("run_on_startup")
    assert not PreferenceStore(tmp_path).load().run_on_startup
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("value", [[], {}, {"schema": 2}, {"schema": 1,
    "run_on_startup": "false", "shutdown_when_traktor_closes": True},
    {"schema": 1, "run_on_startup": False, "shutdown_when_traktor_closes": 0}])
def test_invalid_preferences_are_rejected_and_worker_uses_safe_default(tmp_path, value):
    store = PreferenceStore(tmp_path)
    store.path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        store.load()
    assert store.shutdown_on_close()


def test_save_failure_rolls_back_startup_registration(tmp_path, monkeypatch):
    store, startup = PreferenceStore(tmp_path), Startup()
    def fail(_value):
        raise PermissionError("synthetic denied write")
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(PermissionError):
        DesktopSettings(store, startup).set_startup(True)
    assert not startup.enabled()
    assert store.load() == DesktopPreferences()


def test_preference_redirect_is_rejected_without_changing_destination(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text("untouched")
    store = PreferenceStore(tmp_path / "data")
    store.path.parent.mkdir()
    try:
        store.path.symlink_to(target)
    except OSError:
        pytest.skip("Creating a symlink requires OS permission")
    with pytest.raises(ValueError, match="REDIRECTED"):
        store.save(DesktopPreferences())
    assert target.read_text() == "untouched"


def test_live_worker_choice_survives_multiple_host_sessions_and_reenable(tmp_path):
    store = PreferenceStore(tmp_path)
    store.save(DesktopPreferences(False, False))
    now, state = [0], [HostProcessState.RUNNING]
    worker = WorkerLifetime(SimpleNamespace(state=lambda: state[0]),
        SimpleNamespace(poll=lambda: None), tmp_path / "stop.request",
        clock=lambda: now[0], shutdown_on_close=store.shutdown_on_close)
    for host_state in (HostProcessState.RUNNING, HostProcessState.NOT_RUNNING,
                       HostProcessState.RUNNING, HostProcessState.NOT_RUNNING):
        state[0] = host_state
        now[0] += 20
        assert not worker.should_stop()
    store.save(replace(store.load(), shutdown_when_traktor_closes=True))
    assert not worker.should_stop()
    now[0] += 3
    assert worker.should_stop()


@pytest.mark.parametrize("cause", ["manual", "parent"])
@pytest.mark.parametrize("host_state", list(HostProcessState))
def test_manual_exit_and_parent_loss_keep_host_guard_when_autoexit_disabled(tmp_path, cause, host_state):
    stop = tmp_path / "stop.request"
    if cause == "manual":
        stop.touch()
    now = [0]
    worker = WorkerLifetime(SimpleNamespace(state=lambda: host_state),
        SimpleNamespace(poll=lambda: 0 if cause == "parent" else None), stop,
        clock=lambda: now[0], shutdown_on_close=lambda: False)
    assert not worker.should_stop()
    now[0] = 20
    assert worker.should_stop() is (host_state is HostProcessState.NOT_RUNNING)


class Registry:
    HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 4, 1
    value = None
    writes = 0
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        pass
    def OpenKey(self, *_args):
        return self
    CreateKeyEx = OpenKey
    def QueryValueEx(self, _key, _name):
        if self.value is None:
            raise FileNotFoundError
        return self.value
    def SetValueEx(self, _key, _name, _zero, kind, command):
        self.value = command, kind
        self.writes += 1
    def DeleteValue(self, _key, _name):
        self.value = None
        self.writes += 1


def test_windows_startup_registration_is_quoted_idempotent_and_reversible():
    registry = Registry()
    adapter = WindowsStartup(Path(r"C:\Synthetic User\pythonw.exe"),
                             Path(r"C:\Synthetic User\RekordboxUSB"), registry=registry)
    assert not adapter.enabled()
    adapter.set_enabled(True)
    adapter.set_enabled(True)
    assert registry.writes == 1
    assert adapter.command.startswith('"C:\\Synthetic User\\pythonw.exe" -I -m ')
    assert '--data-root "C:\\Synthetic User\\RekordboxUSB"' in adapter.command
    adapter.set_enabled(False)
    adapter.set_enabled(False)
    assert not adapter.enabled() and registry.writes == 2


@pytest.mark.parametrize("foreign", [("foreign.exe", 1), ("our.exe", 2)])
def test_foreign_startup_registration_is_preserved(foreign):
    registry = Registry()
    registry.value = foreign
    adapter = WindowsStartup(Path("pythonw.exe"), Path("data"), registry=registry)
    for enabled in (False, True):
        with pytest.raises(ValueError, match="COLLISION"):
            adapter.set_enabled(enabled)
    assert registry.value == foreign and registry.writes == 0


@pytest.mark.parametrize("selection", [2, 3])
def test_native_menu_displays_checkmarks_and_persists_selected_preference(tmp_path, selection):
    settings = DesktopSettings(PreferenceStore(tmp_path), Startup())
    owner = WindowsTray(Path("host.exe"), None, startup=True, settings=settings)
    rows, errors = [], []
    user = SimpleNamespace(CreatePopupMenu=lambda: 1,
        AppendMenuW=lambda menu, flags, identifier, label: rows.append((identifier, flags, label)),
        GetCursorPos=lambda _point: True, SetForegroundWindow=lambda _window: None,
        TrackPopupMenu=lambda *_args: selection, PostMessageW=lambda *_args: None,
        DestroyMenu=lambda _menu: None, MessageBoxW=lambda *_args: errors.append(True))
    native = object.__new__(_NativeTray)
    native.owner, native.user = owner, user
    native.c = SimpleNamespace(byref=lambda value: value)
    native.w = SimpleNamespace(POINT=lambda: SimpleNamespace(x=0, y=0))
    native._menu(1)
    assert (2, 0, "Run on startup") in rows
    assert (3, 8, "Shutdown when Traktor closes") in rows
    assert not errors
    persisted = PreferenceStore(tmp_path).load()
    assert persisted.run_on_startup is (selection == 2)
    assert persisted.shutdown_when_traktor_closes is (selection != 3)
    rows.clear()
    user.TrackPopupMenu = lambda *_args: 0
    native._menu(1)
    selected = next(row for row in rows if row[0] == selection)
    assert selected[1] == (8 if selection == 2 else 0)


@pytest.mark.parametrize("state", ["Preparando", "Preparando volumes",
                                   "Aguardando autorização do Windows", "unknown state"])
def test_startup_notice_is_english_operational_and_contains_project_notices(state):
    owner = WindowsTray(Path("host.exe"), "4", startup=True)
    owner.update(state)
    text = owner.startup_text()
    assert text.startswith("TRAKFROM-BX is starting")
    assert "Traktor may already be running." in text
    assert "Wait for this window to become a tray icon." in text
    assert "MIT License" in text and "github.com/gusgustavodj/trakfrom-bx" in text
    assert "independent project" in text
    assert all(name in text for name in ("Native Instruments", "AlphaTheta", "Pioneer DJ", "rekordbox"))
    assert not any(word in text.lower() for word in (
        "instagram", "thanks", "onelibrary", "device library plus", "fork", "preparando", "autorização"))
    assert text.index("Traktor may already be running") < text.index("MIT License") < text.index("independent project")
    assert not owner.stop_requested()


def test_about_opens_reuses_and_closes_without_stopping_tray():
    from rekordbox_usb import __version__
    owner = WindowsTray(Path("host.exe"), "4", startup=True)
    created, destroyed, quit_calls, shown = [], [], [], []
    def create(*args):
        created.append(args)
        return 20 + len(created)
    native = object.__new__(_NativeTray)
    native.owner, native.about_window = owner, None
    native.position, native.class_name, native.window = (0, 0), "synthetic", 1
    native.instance, native.font, native.label, native.added = 1, 1, 5, False
    native.user = SimpleNamespace(CreateWindowExW=create, SendMessageW=lambda *_: None,
        ShowWindow=lambda *args: shown.append(args), SetForegroundWindow=lambda *_: None,
        DestroyWindow=lambda window: destroyed.append(window),
        PostQuitMessage=lambda *_: quit_calls.append(True),
        SetWindowTextW=lambda *_: None, DefWindowProcW=lambda *_: 0)
    assert native._show_about()
    window = native.about_window
    assert len(created) == 2 and created[0][2] == "About TRAKFROM-BX"
    assert created[0][8] is None  # The tray owner is hidden after READY.
    text = created[1][2]
    assert __version__ in text and "MIT License" in text
    assert "github.com/gusgustavodj/trakfrom-bx" in text and "independent project" in text
    assert native._show_about() and len(created) == 2
    assert native._message(1, native.UPDATE, 0, 0) == 0  # Tray still processes updates.
    assert native._message(window, 0x10, 0, 0) == 0
    assert destroyed == [window]
    assert native._message(window, 2, 0, 0) == 0 and native.about_window is None
    assert not quit_calls and not owner.stop_requested()


def test_about_menu_selection_does_not_request_shutdown():
    owner = WindowsTray(Path("host.exe"), None, startup=True)
    rows, opened = [], []
    native = object.__new__(_NativeTray)
    native.owner = owner
    native.user = SimpleNamespace(CreatePopupMenu=lambda: 1,
        AppendMenuW=lambda _menu, _flags, identifier, label: rows.append((identifier, label)),
        GetCursorPos=lambda _: True, SetForegroundWindow=lambda _: None,
        TrackPopupMenu=lambda *_: 4, PostMessageW=lambda *_: None, DestroyMenu=lambda _: None)
    native.c = SimpleNamespace(byref=lambda value: value)
    native.w = SimpleNamespace(POINT=lambda: SimpleNamespace(x=0, y=0))
    native._show_about = lambda: opened.append(True)
    native._menu(1)
    assert (4, "About TRAKFROM-BX") in rows and opened == [True]
    assert not owner.stop_requested()
