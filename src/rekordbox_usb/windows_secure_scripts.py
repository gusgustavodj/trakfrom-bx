"""Privileged DiskPart command storage, independent of user/install directories."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
from pathlib import Path
import uuid

from .source_write_guard import _ByHandleFileInformation


class SecureScriptError(RuntimeError):
    pass


SDDL_DIRECTORY = "O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
SDDL_FILE = "O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)"


def validate_security(owner, protected, entries):
    """Only two explicit full-control ACEs; reject null/inherited/foreign ACLs."""
    if owner not in {"S-1-5-18", "S-1-5-32-544"} or not protected:
        raise SecureScriptError("untrusted secure-storage owner or DACL")
    if len(entries) != 2 or {entry[2] for entry in entries} != {"S-1-5-18", "S-1-5-32-544"}:
        raise SecureScriptError("unexpected secure-storage trustee")
    for kind, flags, sid, mask in entries:
        if kind != 0 or flags & ~3 or mask != 0x1F01FF:
            raise SecureScriptError("unexpected secure-storage ACE")


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]


class _AclSize(ctypes.Structure):
    _fields_ = [("count", w.DWORD), ("used", w.DWORD), ("free", w.DWORD)]


class WindowsSecureScriptIO:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32.dll", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32.dll", use_last_error=True)
        self.ole = ctypes.WinDLL("ole32.dll", use_last_error=True)
        declarations = (
            (self.kernel, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            (self.kernel, "CreateDirectoryW", [w.LPCWSTR, ctypes.c_void_p], w.BOOL),
            (self.kernel, "CloseHandle", [w.HANDLE], w.BOOL),
            (self.kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p),
            (self.kernel, "GetFileInformationByHandle", [w.HANDLE, ctypes.c_void_p], w.BOOL),
            (self.kernel, "GetFinalPathNameByHandleW", [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD], w.DWORD),
            (self.kernel, "WriteFile", [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
            (self.kernel, "FlushFileBuffers", [w.HANDLE], w.BOOL),
            (self.kernel, "DeleteFileW", [w.LPCWSTR], w.BOOL),
            (self.kernel, "GetDriveTypeW", [w.LPCWSTR], w.UINT),
            (self.security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
            (self.security, "GetSecurityInfo", [w.HANDLE, w.DWORD, w.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], w.DWORD),
            (self.security, "GetSecurityDescriptorControl", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
            (self.security, "ConvertSidToStringSidW", [ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
            (self.security, "GetAclInformation", [ctypes.c_void_p, ctypes.c_void_p, w.DWORD, w.DWORD], w.BOOL),
            (self.security, "GetAce", [ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            (self.shell, "SHGetKnownFolderPath", [ctypes.c_void_p, w.DWORD, w.HANDLE, ctypes.c_void_p], ctypes.c_long),
            (self.ole, "CoTaskMemFree", [ctypes.c_void_p], None),
        )
        for library, name, arguments, result in declarations:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    def require_elevated(self):
        if not self.shell.IsUserAnAdmin():
            raise SecureScriptError("secure DiskPart storage requires explicit elevation")

    def root(self):
        guid = ctypes.create_string_buffer(uuid.UUID("62AB5D82-FDC1-4DC3-A9DD-070D1D495D97").bytes_le)
        value = ctypes.c_void_p()
        if self.shell.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(value)) != 0:
            raise SecureScriptError("ProgramData known folder unavailable")
        try:
            path = Path(ctypes.wstring_at(value))
        finally:
            self.ole.CoTaskMemFree(value)
        if not path.is_absolute() or self.kernel.GetDriveTypeW(path.anchor) != 3:
            raise SecureScriptError("ProgramData must be an absolute fixed local location")
        return path / "TRAKFROM-BX" / "secure-runtime"

    @contextmanager
    def attributes(self, directory):
        descriptor = ctypes.c_void_p()
        if not self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            SDDL_DIRECTORY if directory else SDDL_FILE, 1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        finally:
            self.kernel.LocalFree(descriptor)

    def open_directory(self, path, create=False):
        if create:
            with self.attributes(True) as attributes:
                if not self.kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                    error = ctypes.get_last_error()
                    if error != 183:
                        raise ctypes.WinError(error)
        # No FILE_SHARE_DELETE: pin every ancestor throughout the child lifetime.
        return self._open(path, 0x20080, 3, None, 3, 0x02200000)

    def _open(self, path, access, sharing, attributes, creation, flags):
        handle = self.kernel.CreateFileW(str(path), access, sharing, attributes, creation, flags, None)
        if handle == w.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def sid(self, address):
        value = ctypes.c_void_p()
        if not self.security.ConvertSidToStringSidW(address, ctypes.byref(value)):
            raise SecureScriptError("invalid secure-storage SID")
        try:
            return ctypes.wstring_at(value)
        finally:
            self.kernel.LocalFree(value)

    def check_security(self, handle):
        owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        error = self.security.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None,
                                              ctypes.byref(acl), None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        try:
            control, revision = w.WORD(), w.DWORD()
            if not self.security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)) or not acl:
                raise SecureScriptError("missing secure-storage DACL")
            size = _AclSize()
            if not self.security.GetAclInformation(acl, ctypes.byref(size), ctypes.sizeof(size), 2):
                raise SecureScriptError("invalid secure-storage DACL")
            entries = []
            for index in range(size.count):
                ace = ctypes.c_void_p()
                if not self.security.GetAce(acl, index, ctypes.byref(ace)):
                    raise SecureScriptError("invalid secure-storage ACE")
                header = (ctypes.c_ubyte * 8).from_address(ace.value)
                if header[0] != 0:
                    raise SecureScriptError("unsupported secure-storage ACE")
                mask = w.DWORD.from_address(ace.value + 4).value
                entries.append((header[0], header[1], self.sid(ace.value + 8), mask))
            validate_security(self.sid(owner), bool(control.value & 0x1000), entries)
        finally:
            self.kernel.LocalFree(descriptor)

    def check(self, handle, path, directory, restricted):
        info = _ByHandleFileInformation()
        if not self.kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.file_attributes & 0x400 or bool(info.file_attributes & 0x10) != directory:
            raise SecureScriptError("secure-storage reparse or object-type mismatch")
        value = ctypes.create_unicode_buffer(32768)
        length = self.kernel.GetFinalPathNameByHandleW(handle, value, len(value), 0)
        if not 0 < length < len(value) or Path(value.value.removeprefix("\\\\?\\")) != path:
            raise SecureScriptError("secure-storage physical path mismatch")
        if restricted:
            self.check_security(handle)
        return info.volume_serial, info.file_index_high, info.file_index_low

    def create_file(self, path, payload):
        with self.attributes(False) as attributes:
            # CREATE_NEW; OPEN_REPARSE_POINT; no write/delete sharing.
            handle = self._open(path, 0xC0020080, 1, ctypes.byref(attributes), 1, 0x00200000)
        identity = None
        try:
            identity = self.check(handle, path, False, True)
            data = payload.encode("utf-8")
            count = w.DWORD()
            if not self.kernel.WriteFile(handle, data, len(data), ctypes.byref(count), None) or count.value != len(data):
                raise SecureScriptError("incomplete secure-script write")
            if not self.kernel.FlushFileBuffers(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close(handle)
            if identity is not None:
                self.delete(path)
            raise
        else:
            self.close(handle)
        # ACL and pinned parents protect this transition; compare physical IDs.
        handle = self._open(path, 0x80020080, 1, None, 3, 0x00200000)
        try:
            if self.check(handle, path, False, True) != identity:
                raise SecureScriptError("secure-script identity changed while sealing")
            return handle, identity
        except BaseException:
            self.close(handle)
            raise

    def delete(self, path):
        if not self.kernel.DeleteFileW(str(path)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self, handle):
        self.kernel.CloseHandle(handle)


@contextmanager
def secure_diskpart_script(script, boundary, *, io=None):
    """Fail closed; hold ancestor and script handles until the consumer exits."""
    io = io or WindowsSecureScriptIO()
    handles = []
    file_handle = None
    path = None
    identity = None
    try:
        io.require_elevated()
        root = io.root()
        if not root.is_absolute():
            raise SecureScriptError("relative secure-storage location")
        for ancestor in reversed((root, *root.parents)):
            restricted = ancestor in (root.parent, root)
            handle = io.open_directory(ancestor, create=restricted)
            handles.append((handle, ancestor, None))
            handles[-1] = (handle, ancestor, io.check(handle, ancestor, True, restricted))
        path = root / ("diskpart-" + uuid.uuid4().hex + ".txt")
        # Reuse the source guard's physical destination policy without changing
        # VHDX permits or granting a detached-source permit for another path.
        boundary._assert_safe(boundary._snapshot(path))
        file_handle, identity = io.create_file(path, script)
        for handle, ancestor, original in handles:
            if io.check(handle, ancestor, True, ancestor in (root.parent, root)) != original:
                raise SecureScriptError("secure-storage ancestor identity changed")
        if io.check(file_handle, path, False, True) != identity:
            raise SecureScriptError("secure-script identity changed")
        yield path
    finally:
        try:
            if file_handle is not None:
                try:
                    if io.check(file_handle, path, False, True) != identity:
                        raise SecureScriptError("secure-script cleanup identity mismatch")
                finally:
                    io.close(file_handle)
                io.delete(path)
        finally:
            for handle, _, _ in reversed(handles):
                io.close(handle)
