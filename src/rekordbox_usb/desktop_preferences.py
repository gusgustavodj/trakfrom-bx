"""Portable desktop preference contract; platform effects stay in adapters."""
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import threading
import uuid


@dataclass(frozen=True)
class DesktopPreferences:
    run_on_startup: bool = False
    shutdown_when_traktor_closes: bool = True


class PreferenceStore:
    def __init__(self, data_root: Path):
        self.path = data_root / "desktop-preferences.json"

    def _check(self):
        for path in (self.path, *self.path.parents):
            if path.is_symlink() or (path.exists() and
                    getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                raise ValueError("PREFERENCES_REDIRECTED")

    def load(self):
        self._check()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return DesktopPreferences()
        if (not isinstance(value, dict) or value.get("schema") != 1 or
                set(value) != {"schema", "run_on_startup", "shutdown_when_traktor_closes"} or
                any(type(value[name]) is not bool for name in asdict(DesktopPreferences()))):
            raise ValueError("INVALID_DESKTOP_PREFERENCES")
        return DesktopPreferences(value["run_on_startup"], value["shutdown_when_traktor_closes"])

    def save(self, preferences):
        if not isinstance(preferences, DesktopPreferences) or any(
                type(value) is not bool for value in asdict(preferences).values()):
            raise ValueError("INVALID_DESKTOP_PREFERENCES")
        self._check()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(".desktop-preferences-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump({"schema": 1, **asdict(preferences)}, stream, sort_keys=True)
                stream.write("\n")
            self._check()
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def shutdown_on_close(self):
        # A corrupted preference must not strand a worker indefinitely.
        try:
            return self.load().shutdown_when_traktor_closes
        except (OSError, ValueError):
            return True


class DesktopSettings:
    """Serialize UI changes and roll back the platform effect if saving fails."""
    def __init__(self, store, startup):
        self.store, self.startup = store, startup
        self._lock = threading.Lock()

    def current(self):
        return replace(self.store.load(), run_on_startup=self.startup.enabled())

    def set_startup(self, enabled):
        with self._lock:
            previous = self.current()
            self.startup.set_enabled(enabled)
            try:
                self.store.save(replace(previous, run_on_startup=enabled))
            except Exception:
                self.startup.set_enabled(previous.run_on_startup)
                raise

    def toggle(self, name):
        if name == "run_on_startup":
            self.set_startup(not self.current().run_on_startup)
        elif name == "shutdown_when_traktor_closes":
            with self._lock:
                previous = self.current()
                self.store.save(replace(previous, shutdown_when_traktor_closes=
                                        not previous.shutdown_when_traktor_closes))
        else:
            raise ValueError("UNKNOWN_DESKTOP_PREFERENCE")
