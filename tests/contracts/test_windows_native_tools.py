"""Harmless search-poisoning fixtures; no fake executable is ever launched."""
from pathlib import Path
import subprocess

import pytest
from tests.fixtures.secure_script_io import benign_script_storage

from rekordbox_usb import windows_native_tools as tools
from rekordbox_usb.source_write_guard import SourceWriteBoundary, WriteIntent, WritePlan
from rekordbox_usb.windows_virtual_disk_backend import (
    SubprocessPowerShellRunner, WindowsStorageToolUnavailableError,
)
from rekordbox_usb.windows_diskpart_virtual_disk_backend import (
    SubprocessDiskpartRunner, DiskpartToolUnavailableError,
)


@pytest.fixture
def native_tree(tmp_path, monkeypatch):
    monkeypatch.setattr("rekordbox_usb.windows_diskpart_virtual_disk_backend.secure_diskpart_script",
                        benign_script_storage(tmp_path))
    windows = tmp_path / "Windows"
    system = windows / "System32"
    powershell = system / "WindowsPowerShell/v1.0/powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"inert native-path fixture")
    diskpart = system / "diskpart.exe"
    diskpart.write_bytes(b"inert native-path fixture")
    monkeypatch.setattr(tools, "_windows_directories", lambda: (windows, system))
    return windows, system, {"powershell": powershell, "diskpart": diskpart}


def invoke(tool, tmp_path):
    if tool == "powershell":
        return SubprocessPowerShellRunner().run("harmless mocked command", 1)
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "owned.vhdx"
    boundary = SourceWriteBoundary(source)
    permit = boundary.authorize(WritePlan(WriteIntent.VHDX, (target,)))
    try:
        return SubprocessDiskpartRunner().run(
            f'select vdisk file="{target}"\nattach vdisk\n', 1,
            boundary=boundary, vhdx_path=target, vhdx_permit=permit,
        )
    finally:
        permit.close()


@pytest.mark.parametrize("tool", ["powershell", "diskpart"])
@pytest.mark.parametrize("poison", ["PATH", "current_directory"])
def test_contaminated_search_never_selects_homonym(tool, poison, native_tree, tmp_path, monkeypatch):
    windows, system, paths = native_tree
    decoys = tmp_path / "decoys"
    decoys.mkdir()
    fake = decoys / paths[tool].name
    fake.write_bytes(b"inert homonym, never execute")
    if poison == "PATH":
        monkeypatch.setenv("PATH", str(decoys))
    else:
        monkeypatch.chdir(decoys)
    monkeypatch.setenv("SystemRoot", str(decoys))
    monkeypatch.setenv("WINDIR", str(decoys))
    monkeypatch.setenv("PSModulePath", str(decoys))
    calls = []

    def harmless(command, **kwargs):
        calls.append(command)
        assert Path(command[0]) == paths[tool]
        assert kwargs["executable"] == str(paths[tool])
        assert kwargs["env"]["SystemRoot"] == str(windows)
        assert str(decoys) not in kwargs["env"]["PATH"]
        assert kwargs["env"]["PSModulePath"] == str(system / "WindowsPowerShell/v1.0/Modules")
        assert "shell" not in kwargs
        return subprocess.CompletedProcess(command, 0, "mocked", "")

    monkeypatch.setattr(subprocess, "run", harmless)
    assert invoke(tool, tmp_path) == "mocked"
    assert len(calls) == 1
    assert fake.read_bytes() == b"inert homonym, never execute"
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize("tool", ["powershell", "diskpart"])
def test_missing_native_tool_fails_closed_despite_path_homonym(tool, native_tree, tmp_path, monkeypatch):
    _, _, paths = native_tree
    paths[tool].unlink()
    (tmp_path / paths[tool].name).write_bytes(b"inert homonym")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("must not launch any process"))
    error = WindowsStorageToolUnavailableError if tool == "powershell" else DiskpartToolUnavailableError
    with pytest.raises(error):
        invoke(tool, tmp_path)
    if tool == "diskpart":
        assert not SubprocessDiskpartRunner().is_available()
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize("tool", ["powershell", "diskpart"])
@pytest.mark.parametrize("configured", ["bare", "foreign", "relative"])
def test_tool_override_cannot_escape_windows(tool, configured, native_tree, tmp_path):
    _, _, paths = native_tree
    value = {"bare": paths[tool].name, "foreign": str(tmp_path / paths[tool].name),
             "relative": "./" + paths[tool].name}[configured]
    with pytest.raises(tools.NativeWindowsToolUnavailableError):
        tools.resolve_native_tool(tool, value)
    assert tools.resolve_native_tool(tool, str(paths[tool])) == str(paths[tool])


@pytest.mark.parametrize("tool", ["powershell", "diskpart"])
def test_reparse_tool_rejected(tool, native_tree, monkeypatch):
    _, _, paths = native_tree
    original = Path.stat

    def redirected(path, **kwargs):
        info = original(path, **kwargs)
        if path == paths[tool]:
            class Reparse:
                st_mode = info.st_mode
                st_file_attributes = 0x400
            return Reparse()
        return info

    monkeypatch.setattr(Path, "stat", redirected)
    with pytest.raises(tools.NativeWindowsToolUnavailableError, match="redirected"):
        tools.resolve_native_tool(tool)


def test_unexpected_system_directory_fails_closed(native_tree, monkeypatch):
    windows, _, _ = native_tree
    monkeypatch.setattr(tools, "_windows_directories", lambda: (windows, windows / "foreign"))
    with pytest.raises(tools.NativeWindowsToolUnavailableError):
        tools.resolve_native_tool("diskpart")
