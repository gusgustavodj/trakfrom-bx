"""Per-user Windows shortcuts and supervised Traktor/storage lifetime.

Traktor runs with the launcher's normal user token. Only the storage worker
requests UAC; it reuses the existing ownership and conservative host guards.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .host_process import (
    HostProcessState, TRAKTOR_HOST_EXECUTABLES, WindowsTraktorProcessProbe,
    _normalize_windows_executable_path,
)
from .public_runtime import PublicRuntime, PublicRuntimeError
from .public_watch import PublicWatchRuntime, exclusive_runtime
from .util import default_data_root


class LauncherError(PublicRuntimeError):
    pass


@dataclass(frozen=True)
class TraktorInstallation:
    version: str
    executable: Path


def discover_installations(*, program_roots=None, registry_dirs=None):
    """Read exact NI install locations; never search backups or patcher folders."""
    if program_roots is None:
        program_roots = [Path(value) for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")
                         if (value := os.environ.get(name))]
    if registry_dirs is None:
        registry_dirs = {}
        import winreg
        for version in ("2", "3", "4"):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                        rf"SOFTWARE\Native Instruments\Traktor Pro {version}",
                                        0, winreg.KEY_READ | view) as key:
                        directory, _ = winreg.QueryValueEx(key, "InstallDir")
                    if isinstance(directory, str):
                        registry_dirs.setdefault(version, []).append(Path(directory))
                except OSError:
                    pass
    result = []
    for version in ("2", "3", "4"):
        directories = list(registry_dirs.get(version, ()))
        directories.extend(Path(root) / "Native Instruments" / f"Traktor Pro {version}"
                           for root in program_roots)
        name = "Traktor Pro 4.exe" if version == "4" else "Traktor.exe"
        for directory in directories:
            executable = Path(directory) / name
            if executable.is_absolute() and executable.is_file():
                result.append(TraktorInstallation(version, executable.resolve()))
                break
    return tuple(result)


def host_probe(installations):
    specifications = list(TRAKTOR_HOST_EXECUTABLES)
    for installation in installations:
        index = int(installation.version) - 2
        spec = specifications[index]
        if spec.allowed_executable_paths:
            specifications[index] = replace(spec, allowed_executable_paths=(
                spec.allowed_executable_paths | {
                    _normalize_windows_executable_path(str(installation.executable))
                }))
    return WindowsTraktorProcessProbe(specifications=specifications)


def shortcut_records(installations, pythonw: Path, data_root: Path):
    preferred = max(installations, key=lambda item: int(item.version), default=None)
    return [{
        "name": "TRAKFROM-BX.lnk",
        "target": str(pythonw),
        "arguments": subprocess.list2cmdline([
            "-I", "-m", "rekordbox_usb.public_launcher", "service",
            "--data-root", str(data_root),
        ]),
        "working_directory": str(pythonw.parent),
        "icon": str(preferred.executable if preferred else pythonw) + ",0",
        "description": "TRAKFROM-BX USB service launcher",
    }]


_SHORTCUT_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$records = $payload.records
$desktop = [Environment]::GetFolderPath('DesktopDirectory')
if (-not $desktop -or -not (Test-Path -LiteralPath $desktop -PathType Container)) {
    throw 'DESKTOP_UNAVAILABLE'
}
$shell = New-Object -ComObject WScript.Shell
# Check every collision before saving any shortcut.
foreach ($record in $records) {
    $path = Join-Path $desktop $record.name
    if (Test-Path -LiteralPath $path) {
        $existing = $shell.CreateShortcut($path)
        if ($existing.Description -ne $record.description -or
            $existing.Arguments -notlike '*rekordbox_usb.public_launcher*') {
            throw 'SHORTCUT_NAME_COLLISION'
        }
    }
}
foreach ($record in $records) {
    $link = $shell.CreateShortcut((Join-Path $desktop $record.name))
    $link.TargetPath = $record.target
    $link.Arguments = $record.arguments
    $link.WorkingDirectory = $record.working_directory
    $link.IconLocation = $record.icon
    $link.Description = $record.description
    $link.WindowStyle = 1
    $link.Save()
}
# Retire only the old shortcuts positively identified as our launcher. Keep
# recoverable copies locally instead of deleting files or touching foreign links.
foreach ($version in 2,3,4) {
    $oldPath = Join-Path $desktop "TRAKFROM-BX + Traktor Pro $version.lnk"
    if (Test-Path -LiteralPath $oldPath -PathType Leaf) {
        $old = $shell.CreateShortcut($oldPath)
        if ($old.Description -eq 'TRAKFROM-BX supervised Traktor launcher' -and
            $old.Arguments -like '*rekordbox_usb.public_launcher launch*' -and
            $old.Arguments -like "*--traktor-version $version*") {
            New-Item -ItemType Directory -Force -Path $payload.retired_directory | Out-Null
            $archivePath = Join-Path $payload.retired_directory (
                [IO.Path]::GetFileNameWithoutExtension($oldPath) + '-' +
                [Guid]::NewGuid().ToString('N') + '.lnk')
            Move-Item -LiteralPath $oldPath -Destination $archivePath
        }
    }
}
'''


def install_shortcuts(installations, data_root: Path, *, runner=subprocess.run):
    # Packaged desktop apps can redirect LocalAppData writes. Resolve the actual
    # executable so the shortcut also works outside that app's virtualized view.
    pythonw = Path(sys.executable).with_name("pythonw.exe").resolve()
    if not pythonw.is_file():
        raise LauncherError("PYTHONW_UNAVAILABLE")
    records = shortcut_records(installations, pythonw, data_root)
    retired = data_root / "launcher/retired-shortcuts"
    if retired.exists() and (not retired.is_dir() or is_redirect(retired)):
        raise LauncherError("INVALID_DATA_ROOT")
    encoded = base64.b64encode(_SHORTCUT_SCRIPT.encode("utf-16-le")).decode("ascii")
    result = runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                    input=json.dumps({"records": records, "retired_directory": str(retired)}),
                    text=True, capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise LauncherError("SHORTCUT_INSTALL_FAILED")
    return records


class NativeProcess:
    """Query/wait-only handle. No forced termination of host or worker."""
    def __init__(self, handle):
        import ctypes
        from ctypes import wintypes
        self.handle = handle
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)

    def poll(self):
        import ctypes
        from ctypes import wintypes
        state = self.kernel.WaitForSingleObject(self.handle, 0)
        if state == 258:
            return None
        if state != 0:
            raise LauncherError("PROCESS_OBSERVATION_FAILED")
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise LauncherError("PROCESS_OBSERVATION_FAILED")
        return code.value

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def elevate_worker(session: Path, data_root: Path):
    import ctypes
    from ctypes import wintypes
    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]
    parameters = subprocess.list2cmdline([
        "-I", "-B", "-m", "rekordbox_usb.public_launcher", "worker",
        "--session", str(session), "--parent-pid", str(os.getpid()),
        "--data-root", str(data_root),
    ])
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    pythonw = Path(sys.executable).resolve().with_name("pythonw.exe")
    if not pythonw.is_file():
        raise LauncherError("PYTHONW_UNAVAILABLE")
    info.lpVerb, info.lpFile, info.lpParameters = "runas", str(pythonw), parameters
    info.nShow = 0
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = (ctypes.POINTER(ShellExecuteInfo),)
    shell.ShellExecuteExW.restype = wintypes.BOOL
    if not shell.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
        raise LauncherError("STORAGE_ELEVATION_CANCELLED")
    return NativeProcess(info.hProcess)


def parent_process(pid: int):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)  # synchronize/query only
    if not handle:
        raise LauncherError("PARENT_UNAVAILABLE")
    return NativeProcess(handle)


def write_status(session: Path, state: str, **details):
    temporary = session / "status.tmp"
    temporary.write_text(json.dumps({"state": state, **details}), encoding="utf-8")
    temporary.replace(session / "status.json")


def supervise(worker, session: Path, start_host, *, clock=time.monotonic, sleep=time.sleep,
              startup_timeout=120, shutdown_timeout=120, on_state=lambda _: None):
    """Wait for storage readiness before starting host; always request cleanup."""
    try:
        on_state("Preparando volumes")
        deadline = clock() + startup_timeout
        while True:
            if worker.poll() is not None:
                raise LauncherError("STORAGE_STARTUP_FAILED")
            try:
                status = json.loads((session / "status.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                status = {}
            if status.get("state") == "ERROR":
                raise LauncherError(status.get("code", "STORAGE_STARTUP_FAILED"))
            if status.get("state") == "READY":
                break
            if clock() >= deadline:
                raise LauncherError("STORAGE_STARTUP_TIMEOUT")
            sleep(0.25)
        on_state("Abrindo Traktor")
        host = start_host()
        on_state("Ativo")
        while host.poll() is None:
            if worker.poll() is not None:
                raise LauncherError("STORAGE_STOPPED_WHILE_TRAKTOR_RUNNING")
            sleep(0.5)
    finally:
        (session / "stop.request").touch()
        on_state("Encerrando")
    deadline = clock() + shutdown_timeout
    while worker.poll() is None:
        if clock() >= deadline:
            raise LauncherError("STORAGE_CLEANUP_PENDING")
        sleep(0.25)
    if worker.poll() != 0:
        raise LauncherError("STORAGE_CLEANUP_FAILED")
    return 0


class WorkerLifetime:
    def __init__(self, probe, parent, stop_file, *, clock=time.monotonic, close_grace_seconds=3,
                 shutdown_on_close=lambda: True):
        self.probe, self.parent, self.stop_file = probe, parent, stop_file
        self.host_seen = False
        self.shutdown_on_close = shutdown_on_close
        self.clock, self.close_grace_seconds, self.clear_since = clock, close_grace_seconds, None

    def should_stop(self):
        state = self.probe.state()
        if state is HostProcessState.RUNNING:
            self.host_seen = True
        requested = (self.stop_file.exists() or self.parent.poll() is not None or
                     (self.host_seen and self.shutdown_on_close()))
        if not requested or state is not HostProcessState.NOT_RUNNING:
            self.clear_since = None
            return False
        if self.clear_since is None:
            self.clear_since = self.clock()
        return self.clock() - self.clear_since >= self.close_grace_seconds


class WorkerJournal:
    """Separate startup failures, runtime warnings and actual cleanup outcome."""
    ERRORS = {"VOLUME_ERROR", "SOURCE_PREFLIGHT_FAILED", "STARTUP_RECOVERY_ERROR",
              "SHUTDOWN_CLEANUP_ERROR", "USB_UNPUBLICATION_ERROR", "WATCH_ERROR"}
    EVENTS = ERRORS | {"USB_PUBLISHED", "MANAGED_VHDX_RECONCILED", "HOST_PROCESS_STATE_UNKNOWN",
                       "SHUTDOWN_CLEANUP_STARTED", "SHUTDOWN_UNPUBLISHED",
                       "SHUTDOWN_CLEANUP_DEFERRED", "SHUTDOWN_CLEANUP_COMPLETED"}

    def __init__(self, session):
        self.session, self.phase, self.startup_errors = session, "STARTUP", []
        self.warnings = 0
        self.cleanup = None

    def event(self, event):
        blocked = event.event == "VOLUME_IGNORED" and event.details.get("reason") == "PREFLIGHT_BLOCKED"
        diagnostic = event.details.get("diagnostic", {})
        code = (event.details.get("code") or event.details.get("diagnostic_code")
                or (diagnostic.get("code") if isinstance(diagnostic, dict) else None))
        if event.event in self.ERRORS or blocked:
            if self.phase == "STARTUP":
                self.startup_errors.append(code or event.details.get("reason") or "STORAGE_STARTUP_FAILED")
            elif self.phase == "READY":
                self.warnings += 1
        if event.event == "SHUTDOWN_CLEANUP_STARTED":
            self.phase = "CLEANUP"
            write_status(self.session, "STOPPING", code="CLEANUP_STARTED")
        if event.event == "SHUTDOWN_CLEANUP_COMPLETED":
            self.cleanup = {name: event.details.get(name, 0) for name in
                            ("cleaned", "already_detached", "errors", "deferred")}
        if event.event in self.EVENTS or blocked:
            record = {"event": event.event, "phase": self.phase}
            # Never store raw exceptions, scripts, paths, titles or library metadata.
            for name in ("reason", "source_id", "cleaned", "already_detached", "errors", "deferred"):
                if name in event.details:
                    record[name] = event.details[name]
            if code:
                record["code"] = code
            with (self.session / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")

    def ready(self):
        if self.startup_errors:
            raise LauncherError(str(self.startup_errors[0]))
        self.phase = "READY"
        write_status(self.session, "READY", code="SERVICE_READY")

    def finish(self, result, remaining):
        clean = (result == 0 and self.cleanup is not None and not remaining
                 and self.cleanup["errors"] == 0 and self.cleanup["deferred"] == 0)
        write_status(self.session, "STOPPED" if clean else "ERROR",
                     code="CLEANUP_COMPLETE" if clean else "STORAGE_CLEANUP_FAILED",
                     cleanup=self.cleanup, remaining_publications=remaining,
                     runtime_warnings=self.warnings)
        return 0 if clean else 3


def validate_root(data_root: Path):
    """Launcher IPC and storage are restricted to the existing per-user local root."""
    expected = default_data_root().resolve()
    if not data_root.is_absolute() or data_root.resolve() != expected:
        raise LauncherError("LAUNCHER_REQUIRES_LOCAL_DEFAULT_DATA_ROOT")
    # Reject redirects/junctions to another volume, including removable media.
    if expected.drive.casefold() != Path(os.environ["LOCALAPPDATA"]).resolve().drive.casefold():
        raise LauncherError("INVALID_DATA_ROOT")
    for path in (data_root, data_root / "launcher"):
        if path.exists() and is_redirect(path):
            raise LauncherError("INVALID_DATA_ROOT")


def is_redirect(path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def run_worker(session: Path, data_root: Path, pid: int, installations):
    validate_root(data_root)
    if session.parent.resolve() != (data_root / "launcher").resolve() or not session.is_dir():
        raise LauncherError("INVALID_LAUNCHER_SESSION")
    if is_redirect(session):
        raise LauncherError("INVALID_LAUNCHER_SESSION")
    parent = parent_process(pid)
    probe = host_probe(installations)
    from .desktop_preferences import PreferenceStore
    lifetime = WorkerLifetime(probe, parent, session / "stop.request",
                              shutdown_on_close=PreferenceStore(data_root).shutdown_on_close)
    journal = WorkerJournal(session)
    runtime = PublicRuntime(host_probe=probe)
    try:
        watch = PublicWatchRuntime(runtime, data_root, event_sink=journal.event,
                                   stop_requested=lifetime.should_stop,
                                   on_startup_complete=journal.ready)
        result = watch.run()
        return journal.finish(result, len(watch.watcher.active_publications))
    except Exception as exc:
        write_status(session, "ERROR", code=getattr(exc, "code", "STORAGE_OPERATION_FAILED"))
        return 3
    finally:
        parent.close()


def read_status(session):
    try:
        value = json.loads((session / "status.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def supervise_service(worker, session, ui, probe, *, clock=time.monotonic,
                      sleep=time.sleep, startup_timeout=120, shutdown_on_close=lambda: True):
    """Prepare storage, then wait for a Traktor opened independently by the user."""
    ready, stopping, seen = False, False, False
    deadline = clock() + startup_timeout
    try:
        while True:
            code = worker.poll()
            status = read_status(session)
            if code is not None:
                if code == 0 and status.get("state") == "STOPPED":
                    return 0
                raise LauncherError(status.get("code", "STORAGE_OPERATION_FAILED"))
            if not ready:
                if status.get("state") == "ERROR":
                    raise LauncherError(status.get("code", "STORAGE_STARTUP_FAILED"))
                if status.get("state") == "READY":
                    ui.mark_ready()
                    ready = True
                elif clock() >= deadline:
                    raise LauncherError("STORAGE_STARTUP_TIMEOUT")
            if ui.stop_requested() and not stopping:
                (session / "stop.request").touch()
                ui.update("Encerramento solicitado; feche o Traktor")
                stopping = True
            if ready and not stopping:
                state = probe.state()
                if state is HostProcessState.RUNNING:
                    ui.update("Ativo")
                    seen = True
                elif seen and state is HostProcessState.NOT_RUNNING:
                    ui.update("Encerrando volumes" if shutdown_on_close() else
                              "Em espera; pode abrir o Traktor novamente")
                elif state is HostProcessState.UNKNOWN:
                    ui.update("Aguardando confirmação do estado do Traktor")
            if status.get("state") == "STOPPING":
                ui.update("Desmontando volumes")
            sleep(0.5)
    finally:
        (session / "stop.request").touch()


def service(installations, data_root, *, ui_factory=None):
    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin():
        raise LauncherError("START_SHORTCUT_AS_NORMAL_USER")
    validate_root(data_root)
    probe = host_probe(installations)
    if probe.state() is not HostProcessState.NOT_RUNNING:
        raise LauncherError("CLOSE_TRAKTOR_BEFORE_LAUNCH")
    if ui_factory is None:
        from .windows_tray import WindowsTray
        from .desktop_preferences import DesktopSettings, PreferenceStore
        from .windows_startup import WindowsStartup
        settings = DesktopSettings(PreferenceStore(data_root), WindowsStartup(
            Path(sys.executable).resolve().with_name("pythonw.exe"), data_root))
        ui_factory = lambda *args, **kwargs: WindowsTray(*args, settings=settings, **kwargs)
    preferred = max(installations, key=lambda item: int(item.version), default=None)
    with exclusive_runtime(data_root / "launcher"):
        ui = ui_factory(preferred.executable if preferred else Path(sys.executable), None, startup=True)
        try:
            ui.start()
            preflight_sources(PublicRuntime(host_probe=probe, capability_probe=lambda: True), data_root)
            if ui.stop_requested():
                return 0
            session = data_root / "launcher" / uuid.uuid4().hex
            session.mkdir(parents=True)
            ui.update("Aguardando autorização do Windows")
            worker = elevate_worker(session, data_root)
            try:
                from .desktop_preferences import PreferenceStore
                return supervise_service(worker, session, ui, probe,
                                         shutdown_on_close=PreferenceStore(data_root).shutdown_on_close)
            finally:
                worker.close()
        finally:
            ui.close()


def launch(installation, installations, data_root, *, tray_factory=None):
    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin():
        raise LauncherError("START_SHORTCUT_AS_NORMAL_USER")
    validate_root(data_root)
    probe = host_probe(installations)
    if probe.state() is not HostProcessState.NOT_RUNNING:
        raise LauncherError("CLOSE_TRAKTOR_BEFORE_LAUNCH")
    if tray_factory is None:
        from .windows_tray import WindowsTray
        tray_factory = WindowsTray
    tray = tray_factory(installation.executable, installation.version)
    try:
        tray.start()
        # Validate sources read-only before requesting any privileged storage effect.
        # The elevated worker repeats preflight with the real capability probe.
        preflight_sources(PublicRuntime(host_probe=probe, capability_probe=lambda: True), data_root)
        session = data_root / "launcher" / uuid.uuid4().hex
        session.mkdir(parents=True)
        tray.update("Aguardando autorização do Windows")
        worker = elevate_worker(session, data_root)
        try:
            return supervise(worker, session, lambda: subprocess.Popen(
                [str(installation.executable)], cwd=installation.executable.parent),
                on_state=tray.update)
        finally:
            worker.close()
    finally:
        tray.close()


def preflight_sources(runtime, data_root):
    for volume in runtime._volumes():
        if not runtime.eligible_volume(volume, data_root):
            continue
        result = runtime.preflight(volume.root, data_root)
        if not result.ready and result.code != "UNSUPPORTED_LIBRARY":
            raise LauncherError(result.code)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="trakfrom-bx launcher")
    parser.add_argument("command", choices=("launch", "service", "install-shortcuts", "worker",
                                            "configure-startup"))
    parser.add_argument("--enabled", choices=("yes", "no"))
    parser.add_argument("--traktor-version", choices=("2", "3", "4"))
    parser.add_argument("--data-root", type=Path, default=default_data_root())
    parser.add_argument("--session", type=Path)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args(argv)
    try:
        if sys.platform != "win32":
            raise LauncherError("UNSUPPORTED_PLATFORM")
        installations = discover_installations()
        if args.command == "configure-startup":
            validate_root(args.data_root)
            if args.enabled is None:
                raise LauncherError("STARTUP_CHOICE_REQUIRED")
            from .desktop_preferences import DesktopSettings, PreferenceStore
            from .windows_startup import WindowsStartup
            DesktopSettings(PreferenceStore(args.data_root), WindowsStartup(
                Path(sys.executable).resolve().with_name("pythonw.exe"), args.data_root
            )).set_startup(args.enabled == "yes")
            return 0
        if args.command == "install-shortcuts":
            validate_root(args.data_root)
            records = install_shortcuts(installations, args.data_root)
            if sys.stdout:
                print(json.dumps({"shortcut_count": len(records)}, sort_keys=True))
            return 0
        if args.command == "worker":
            if args.session is None or args.parent_pid is None:
                raise LauncherError("INVALID_LAUNCHER_SESSION")
            return run_worker(args.session, args.data_root, args.parent_pid, installations)
        if args.command == "service":
            return service(installations, args.data_root)
        installation = next((item for item in installations if item.version == args.traktor_version), None)
        if installation is None:
            raise LauncherError("TRAKTOR_NOT_INSTALLED")
        return launch(installation, installations, args.data_root)
    except Exception as exc:
        code = getattr(exc, "code", "LAUNCHER_OPERATION_FAILED")
        if sys.stderr:
            print(code, file=sys.stderr)
        else:
            import ctypes
            messages = {
                "CLOSE_TRAKTOR_BEFORE_LAUNCH": "Feche todas as versões do Traktor antes de usar este atalho.",
                "START_SHORTCUT_AS_NORMAL_USER": "Abra o atalho normalmente, sem Executar como administrador.",
                "STORAGE_ELEVATION_CANCELLED": "A autorização do Windows foi cancelada. O Traktor não foi iniciado.",
                "STORAGE_CLEANUP_PENDING": "O encerramento aguarda o fechamento de todos os Traktors ou a confirmação de que estão fechados.",
                "TRAKTOR_NOT_INSTALLED": "A versão selecionada do Traktor não foi encontrada. Execute novamente o instalador.",
                "TRAY_UNAVAILABLE": "Não foi possível iniciar o indicador na bandeja do Windows. Tente abrir novamente o atalho.",
                "STORAGE_CLEANUP_FAILED": "Não foi possível concluir a desmontagem de todos os volumes. Feche o Traktor e reinicie o TRAKFROM-BX para tentar a recuperação segura.",
                "RUNTIME_ALREADY_RUNNING": "O TRAKFROM-BX já está em execução. Confira o ícone junto ao relógio.",
            }
            message = messages.get(code, "Não foi possível concluir a operação. Os volumes foram preservados quando necessário.")
            ctypes.windll.user32.MessageBoxW(None, message + "\n\n" + str(code), "TRAKFROM-BX", 0x10)
        return 3


if __name__ == "__main__":
    sys.exit(main())
