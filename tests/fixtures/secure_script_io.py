"""Inert secure-storage model: no elevation and no DiskPart execution."""
from pathlib import Path
from rekordbox_usb.windows_secure_scripts import SecureScriptError, validate_security


class BenignSecureIO:
    def __init__(self, root):
        self.location = root
        self.handles = {}
        self.closed = []
        self.created = []
        self.deleted = []
        self.reparse = set()
        self.foreign_acl = False
        self.changed = None
        self.checks = 0

    def require_elevated(self):
        pass

    def root(self):
        return self.location

    def open_directory(self, path, create=False):
        if create:
            path.mkdir(exist_ok=True)
        self.handles[path] = path
        return path

    def check(self, handle, path, directory, restricted):
        self.checks += 1
        if path in self.reparse:
            raise SecureScriptError("reparse substitution blocked")
        if restricted:
            entries = [(0, 0, "S-1-5-18", 0x1F01FF), (0, 0, "S-1-5-32-544", 0x1F01FF)]
            if self.foreign_acl:
                entries.append((0, 0, "S-1-5-32-545", 0x1F01FF))
            validate_security("S-1-5-32-544", True, entries)
        return (1, hash(path), self.checks if path == self.changed else 0)

    def create_file(self, path, payload):
        with path.open("x", encoding="utf-8") as stream:
            stream.write(payload)
        self.created.append(path)
        self.handles[path] = path
        return path, self.check(path, path, False, True)

    def unprivileged_write(self, path):
        # The model grants access only to SYSTEM/Administrators and also models
        # retained no-write/no-delete handles. It never mutates command bytes.
        if path in self.handles and path not in self.closed:
            raise PermissionError("ACL/held handle denies unprivileged replacement")
        raise AssertionError("missing lifetime protection")

    def delete(self, path):
        path.unlink()
        self.deleted.append(path)

    def close(self, handle):
        self.closed.append(handle)


def benign_script_storage(tmp_path):
    """Test adapter installed explicitly by tests, never selected by product."""
    from contextlib import contextmanager
    from rekordbox_usb.windows_secure_scripts import secure_diskpart_script

    @contextmanager
    def storage(script, boundary):
        root = tmp_path / "privileged-model" / "secure-runtime"
        with secure_diskpart_script(script, boundary, io=BenignSecureIO(root)) as path:
            yield path
    return storage
