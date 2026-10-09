"""Authorial Win32 notification-area indicator, without a console or GUI dependency.

One hidden window pumps Shell notifications on its own thread. Preferences are
persisted through a platform adapter; storage cleanup remains host-aware.
"""
from __future__ import annotations

from pathlib import Path
import threading
import uuid
from . import __version__


PROJECT_NOTICE = (
    "TRAKFROM-BX is open-source software licensed under the MIT License.\n"
    "Source code: github.com/gusgustavodj/trakfrom-bx\n\n"
    "TRAKFROM-BX is an independent project and is not affiliated with,\n"
    "sponsored by, approved by, or endorsed by Native Instruments,\n"
    "AlphaTheta, Pioneer DJ, or rekordbox."
)

DISPLAY_STATES = {
    "Preparando": "Preparing TRAKFROM-BX...",
    "Preparando volumes": "Preparing volumes...",
    "Aguardando autorização do Windows": "Waiting for Windows authorization...",
    "Pronto; pode abrir o Traktor": "Ready; you can open Traktor",
    "Abrindo Traktor": "Opening Traktor...",
    "Ativo": "Active",
    "Encerrando": "Shutting down...",
    "Encerramento solicitado; feche o Traktor": "Shutdown requested; close Traktor",
    "Encerrando volumes": "Closing volumes...",
    "Em espera; pode abrir o Traktor novamente": "Standing by; you can open Traktor again",
    "Aguardando confirmação do estado do Traktor": "Waiting for Traktor status confirmation...",
    "Desmontando volumes": "Unmounting volumes...",
}


class TrayUnavailableError(RuntimeError):
    code = "TRAY_UNAVAILABLE"


class WindowsTray:
    def __init__(self, executable: Path, version: str | None, *, startup=False, settings=None):
        self.executable = executable
        self.version = version
        self.settings = settings
        self._state = "Preparando"
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread = None
        self._native = None
        self._error = None
        self._startup = startup
        self._service_ui = startup
        self._closing = threading.Event()
        self._stop = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="TRAKFROM-BX tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(5) or self._error:
            self.close()
            raise TrayUnavailableError("Windows notification area is unavailable")
        return self

    def update(self, state: str):
        with self._lock:
            if self._state == state:
                return
            self._state = state
        if self._native:
            self._native.post_update()

    def mark_ready(self):
        self.update("Pronto; pode abrir o Traktor")
        self._startup = False
        if self._native:
            self._native.post_ready()

    def stop_requested(self):
        return self._stop.is_set()

    def preference_items(self):
        if self.settings is None:
            return ()
        current = self.settings.current()
        return ((2, "run_on_startup", "Run on startup", current.run_on_startup),
                (3, "shutdown_when_traktor_closes", "Shutdown when Traktor closes",
                 current.shutdown_when_traktor_closes))

    def select_preference(self, selected):
        for identifier, name, _label, _checked in self.preference_items():
            if identifier == selected:
                self.settings.toggle(name)
                return True
        return False

    def text(self):
        with self._lock:
            host = f" · Traktor Pro {self.version}" if self.version else ""
            return f"TRAKFROM-BX{host} · {DISPLAY_STATES.get(self._state, self._state)}"

    def startup_text(self):
        with self._lock:
            status = DISPLAY_STATES.get(self._state, "Preparing TRAKFROM-BX...")
        return ("TRAKFROM-BX is starting\n\n" + status + "\n\n"
                "Open Traktor when this window becomes a tray icon.\n\n" + PROJECT_NOTICE)

    def about_text(self):
        return (f"TRAKFROM-BX\nVersion {__version__}\n\n" + PROJECT_NOTICE +
                "\n\nTraktor and rekordbox are trademarks of their respective owners.")

    def close(self):
        self._closing.set()
        if self._native:
            self._native.post_close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def _run(self):
        native = None
        try:
            native = _NativeTray(self)
            self._native = native
            self._ready.set()
            native.run()
        except Exception as exc:
            self._error = exc
        finally:
            if native:
                native.close()
            self._native = None
            self._ready.set()


class _NativeTray:
    UPDATE = 0x8001
    CALLBACK = 0x8002
    READY = 0x8003

    def __init__(self, owner):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w, self.owner = c, w, owner
        self.window, self.icon, self.added, self.registered = None, None, False, False
        self.label = None
        self.about_window = None
        self.class_name = "TRAKFROM_BX_TRAY_" + uuid.uuid4().hex
        self.user = c.WinDLL("user32", use_last_error=True)
        self.shell = c.WinDLL("shell32", use_last_error=True)
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes, kernel.GetModuleHandleW.restype = (w.LPCWSTR,), w.HMODULE
        self.instance = kernel.GetModuleHandleW(None)
        procedure = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        class WindowClass(c.Structure):
            _fields_ = [("style", w.UINT), ("lpfnWndProc", procedure),
                        ("cbClsExtra", c.c_int), ("cbWndExtra", c.c_int),
                        ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                        ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                        ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]
        class NotifyIcon(c.Structure):
            _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT),
                        ("uFlags", w.UINT), ("uCallbackMessage", w.UINT), ("hIcon", w.HICON),
                        ("szTip", w.WCHAR * 128), ("dwState", w.DWORD),
                        ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                        ("uTimeoutOrVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64),
                        ("dwInfoFlags", w.DWORD), ("guidItem", c.c_byte * 16),
                        ("hBalloonIcon", w.HICON)]
        # Declare pointer-sized arguments explicitly for both 32 and 64-bit Python.
        self.user.RegisterClassW.argtypes, self.user.RegisterClassW.restype = (c.POINTER(WindowClass),), w.WORD
        self.user.UnregisterClassW.argtypes = (w.LPCWSTR, w.HINSTANCE)
        self.user.CreateWindowExW.argtypes = (w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                            c.c_int, c.c_int, c.c_int, c.c_int,
                                            w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p)
        self.user.CreateWindowExW.restype = w.HWND
        self.user.DefWindowProcW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        self.user.DefWindowProcW.restype = c.c_ssize_t
        self.user.PostMessageW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        self.user.DestroyWindow.argtypes = (w.HWND,)
        self.user.PostQuitMessage.argtypes = (c.c_int,)
        self.user.GetMessageW.argtypes = (c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
        self.user.GetMessageW.restype = c.c_int
        self.user.TranslateMessage.argtypes = (c.POINTER(w.MSG),)
        self.user.DispatchMessageW.argtypes = (c.POINTER(w.MSG),)
        self.user.DispatchMessageW.restype = c.c_ssize_t
        self.user.RegisterWindowMessageW.argtypes = (w.LPCWSTR,)
        self.user.RegisterWindowMessageW.restype = w.UINT
        self.user.DestroyIcon.argtypes = (w.HICON,)
        self.user.LoadIconW.argtypes, self.user.LoadIconW.restype = (w.HINSTANCE, c.c_void_p), w.HICON
        self.user.CreatePopupMenu.restype = w.HMENU
        self.user.AppendMenuW.argtypes = (w.HMENU, w.UINT, c.c_size_t, w.LPCWSTR)
        self.user.TrackPopupMenu.argtypes = (w.HMENU, w.UINT, c.c_int, c.c_int,
                                           c.c_int, w.HWND, c.c_void_p)
        self.user.TrackPopupMenu.restype = w.UINT
        self.user.DestroyMenu.argtypes = (w.HMENU,)
        self.user.GetCursorPos.argtypes = (c.POINTER(w.POINT),)
        self.user.SetForegroundWindow.argtypes = (w.HWND,)
        self.user.ShowWindow.argtypes = (w.HWND, c.c_int)
        self.user.SetWindowTextW.argtypes = (w.HWND, w.LPCWSTR)
        self.user.MessageBoxW.argtypes = (w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT)
        self.user.SystemParametersInfoW.argtypes = (w.UINT, w.UINT, c.c_void_p, w.UINT)
        self.user.GetSysColorBrush.argtypes, self.user.GetSysColorBrush.restype = (c.c_int,), w.HBRUSH
        self.user.SendMessageW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        self.user.SendMessageW.restype = c.c_ssize_t
        self.shell.Shell_NotifyIconW.argtypes = (w.DWORD, c.POINTER(NotifyIcon))
        self.shell.Shell_NotifyIconW.restype = w.BOOL
        self.shell.ExtractIconExW.argtypes = (w.LPCWSTR, c.c_int, c.POINTER(w.HICON),
                                            c.POINTER(w.HICON), w.UINT)
        self.shell.ExtractIconExW.restype = w.UINT
        self.taskbar_created = self.user.RegisterWindowMessageW("TaskbarCreated")
        self.procedure = procedure(self._message)  # Keep the callback alive through cleanup.
        window_class = WindowClass(lpfnWndProc=self.procedure, hInstance=self.instance,
                                   lpszClassName=self.class_name, hbrBackground=self.user.GetSysColorBrush(15))
        self.data = NotifyIcon(cbSize=c.sizeof(NotifyIcon), uID=1, uFlags=1 | 2 | 4,
                               uCallbackMessage=self.CALLBACK)
        self.owned_icon = False
        try:
            if not self.user.RegisterClassW(c.byref(window_class)):
                raise c.WinError(c.get_last_error())
            self.registered = True
            area = w.RECT()
            if not self.user.SystemParametersInfoW(48, 0, c.byref(area), 0):
                raise c.WinError(c.get_last_error())
            width, height = 660, 360
            left = area.left + (area.right - area.left - width) // 2
            top = area.top + (area.bottom - area.top - height) // 2
            self.position = (left, top)
            # A nonmodal startup notice; no tray icon until storage reports READY.
            style = 0x80C80000 if owner._startup else 0  # Popup, caption and close button.
            self.window = self.user.CreateWindowExW(0, self.class_name, "TRAKFROM-BX", style,
                                                    left, top, width, height, None, None, self.instance, None)
            if not self.window:
                raise c.WinError(c.get_last_error())
            icon = w.HICON()
            if self.shell.ExtractIconExW(str(owner.executable), 0, None, c.byref(icon), 1) == 1 and icon:
                self.icon, self.owned_icon = icon, True
            else:
                self.icon = self.user.LoadIconW(None, c.c_void_p(32512))
            self.data.hWnd, self.data.hIcon = self.window, self.icon
            gdi = c.WinDLL("gdi32", use_last_error=True)
            gdi.GetStockObject.argtypes, gdi.GetStockObject.restype = (c.c_int,), w.HANDLE
            self.font = gdi.GetStockObject(17)
            if owner._startup:
                self.label = self.user.CreateWindowExW(0, "STATIC", "", 0x50000001,
                                                       20, 20, 620, 290, self.window, None,
                                                       self.instance, None)
                if not self.label:
                    raise c.WinError(c.get_last_error())
                self.user.SendMessageW(self.label, 0x30, self.font, 1)
                self._refresh_notice()
                self.user.ShowWindow(self.window, 1)
                self.user.SetForegroundWindow(self.window)
            else:
                self._notify(0)
        except BaseException:
            self.close()
            raise

    def _notify(self, operation):
        self.data.szTip = self.owner.text()[:127]
        result = self.shell.Shell_NotifyIconW(operation, self.c.byref(self.data))
        if operation == 0:
            self.added = self.added or bool(result)
            if not result:
                raise TrayUnavailableError("Shell rejected notification icon")

    def _refresh_notice(self):
        if self.label:
            self.user.SetWindowTextW(self.label, self.owner.startup_text())

    def _show_about(self):
        # A modeless window shares the tray message pump; no waiting dialog.
        if not self.about_window:
            left, top = self.position
            self.about_window = self.user.CreateWindowExW(
                0, self.class_name, "About TRAKFROM-BX", 0x80C80000,
                left, top, 660, 360, None, None, self.instance, None)
            if not self.about_window:
                return False
            label = self.user.CreateWindowExW(
                0, "STATIC", self.owner.about_text(), 0x50000001,
                20, 20, 620, 290, self.about_window, None, self.instance, None)
            if not label:
                self.user.DestroyWindow(self.about_window)
                self.about_window = None
                return False
            self.user.SendMessageW(label, 0x30, self.font, 1)
        self.user.ShowWindow(self.about_window, 1)
        self.user.SetForegroundWindow(self.about_window)
        return True

    def _message(self, window, message, wparam, lparam):
        # Exceptions must never cross the Win32 callback boundary.
        try:
            if window == self.about_window:
                if message == 0x10:
                    self.user.DestroyWindow(window)
                    return 0
                if message == 2:
                    self.about_window = None
                    return 0
                return self.user.DefWindowProcW(window, message, wparam, lparam)
            if message == self.UPDATE:
                if self.added:
                    self._notify(1)
                else:
                    self._refresh_notice()
                return 0
            if message == self.READY:
                self._notify(0) if not self.added else self._notify(1)
                self.user.ShowWindow(window, 0)
                return 0
            if message == self.taskbar_created:
                if self.added:
                    self._notify(0)
                return 0
            if message == self.CALLBACK:
                if lparam in (0x203, 0x205):  # Left double-click or right-click.
                    self._menu(window)
                return 0
            if message == 0x10:  # WM_CLOSE
                if self.owner._service_ui and not self.owner._closing.is_set():
                    self.owner._stop.set()
                    self.user.ShowWindow(window, 0)
                    return 0
                self.user.DestroyWindow(window)
                return 0
            if message == 2:  # WM_DESTROY
                self.user.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return self.user.DefWindowProcW(window, message, wparam, lparam)

    def _menu(self, window):
        menu = self.user.CreatePopupMenu()
        if not menu:
            return
        try:
            self.user.AppendMenuW(menu, 1, 0, self.owner.text())  # Disabled status row.
            self.user.AppendMenuW(menu, 0x800, 0, None)  # Separator.
            try:
                for identifier, _name, label, checked in self.owner.preference_items():
                    self.user.AppendMenuW(menu, 8 if checked else 0, identifier, label)
            except Exception:
                self.user.AppendMenuW(menu, 1, 0, "Preferências indisponíveis")
            self.user.AppendMenuW(menu, 0x800, 0, None)
            self.user.AppendMenuW(menu, 0, 4, "About TRAKFROM-BX")
            if self.owner._service_ui:
                self.user.AppendMenuW(menu, 1, 0, "Abra o Traktor pelo atalho dele quando estiver pronto.")
                self.user.AppendMenuW(menu, 0, 1, "Encerrar TRAKFROM-BX")
            self.user.AppendMenuW(menu, 1, 0, "O encerramento aguarda o Traktor fechar.")
            self.user.AppendMenuW(menu, 1, 0, "Os volumes serão desmontados automaticamente.")
            point = self.w.POINT()
            if not self.user.GetCursorPos(self.c.byref(point)):
                return
            self.user.SetForegroundWindow(window)
            selected = self.user.TrackPopupMenu(menu, 0x100 | 2, point.x, point.y, 0, window, None)
            self.user.PostMessageW(window, 0, 0, 0)
            if selected == 1:
                self.owner._stop.set()
            elif selected == 4:
                self._show_about()
            elif selected in (2, 3):
                try:
                    self.owner.select_preference(selected)
                except Exception:
                    self.user.MessageBoxW(window, "Não foi possível salvar a preferência. "
                                         "Confira as permissões e tente novamente.", "TRAKFROM-BX", 0x10)
        finally:
            self.user.DestroyMenu(menu)

    def post_update(self):
        if self.window:
            self.user.PostMessageW(self.window, self.UPDATE, 0, 0)

    def post_close(self):
        if self.window:
            self.user.PostMessageW(self.window, 0x10, 0, 0)

    def post_ready(self):
        if self.window:
            self.user.PostMessageW(self.window, self.READY, 0, 0)

    def run(self):
        message = self.w.MSG()
        while True:
            result = self.user.GetMessageW(self.c.byref(message), None, 0, 0)
            if result == 0:
                return
            if result < 0:
                raise self.c.WinError(self.c.get_last_error())
            self.user.TranslateMessage(self.c.byref(message))
            self.user.DispatchMessageW(self.c.byref(message))

    def close(self):
        if self.about_window:
            self.user.DestroyWindow(self.about_window)
            self.about_window = None
        if self.added:
            self.shell.Shell_NotifyIconW(2, self.c.byref(self.data))  # NIM_DELETE
            self.added = False
        if self.window:
            self.user.DestroyWindow(self.window)
            self.window = None
        if self.icon and self.owned_icon:
            self.user.DestroyIcon(self.icon)
            self.icon = None
        if self.registered:
            self.user.UnregisterClassW(self.class_name, self.instance)
            self.registered = False
