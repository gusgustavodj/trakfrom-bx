"""Authorial per-user Windows logon adapter, using only the standard library."""
import subprocess
from pathlib import Path


class WindowsStartup:
    KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    NAME = "TRAKFROM-BX"

    def __init__(self, pythonw, data_root, *, registry=None):
        if registry is None:
            import winreg as registry
        self.registry = registry
        arguments = [str(pythonw), "-I", "-m", "rekordbox_usb.public_launcher", "service"]
        # The private runtime's build marker selects a fixed command.
        # The normal user's canonical data root is resolved at login, never stored
        # as an administrator's profile path by the elevated Setup process.
        bundled = (Path(pythonw).parent / "trakfrom-bx-runtime.json").is_file()
        if not bundled:
            arguments += ["--data-root", str(data_root)]
        self.command = ('"' + str(pythonw) + '" -I -B -m rekordbox_usb.public_launcher service'
                        if bundled else subprocess.list2cmdline(arguments))

    def _value(self):
        r = self.registry
        try:
            with r.OpenKey(r.HKEY_CURRENT_USER, self.KEY, 0, r.KEY_READ) as key:
                value, kind = r.QueryValueEx(key, self.NAME)
        except FileNotFoundError:
            return None
        if kind != r.REG_SZ or value != self.command:
            raise ValueError("STARTUP_NAME_COLLISION")
        return value

    def enabled(self):
        return self._value() is not None

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("INVALID_STARTUP_PREFERENCE")
        present = self.enabled()
        if present == enabled:
            return
        r = self.registry
        with r.CreateKeyEx(r.HKEY_CURRENT_USER, self.KEY, 0, r.KEY_SET_VALUE) as key:
            if enabled:
                r.SetValueEx(key, self.NAME, 0, r.REG_SZ, self.command)
            else:
                r.DeleteValue(key, self.NAME)
