"""Real Win32 frontend checks against synthetic install folders, never storage."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import zipfile

import pytest

@pytest.fixture(scope="module")
def native_bins(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    tools = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
    compiler = subprocess.check_output([
        str(tools / "Microsoft Visual Studio/Installer/vswhere.exe"), "-products", "*",
        "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-find",
        r"VC\Tools\MSVC\**\bin\Hostx64\x64\cl.exe",
    ], text=True).strip().splitlines()[-1]
    spec = importlib.util.spec_from_file_location("frontend_build", root / "packaging/windows/build_installer.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    directory = tmp_path_factory.mktemp("native-frontends")
    build.compile_frontends(directory, Path(compiler), tools / "Windows Kits/10")
    return directory


def installation(tmp_path, native_bins, *, marked=True):
    root = tmp_path / "DJ Synthetic Unicode ç"
    root.mkdir()
    for name in ("TRAKFROM-BX.exe", "Uninstall-TRAKFROM-BX.exe"):
        shutil.copyfile(native_bins / name, root / name)
    if marked:
        (root / "install-marker.ini").write_text(
            "[installation]\nproduct=TRAKFROM-BX\n"
            "app_id={574E3A6C-90F3-489F-A34F-C1D1A3E8609F}\n"
            "schema=1\ninstallation_id=synthetic-id\npath=" + str(root) + "\n",
            encoding="utf-16")
    return root


@pytest.mark.parametrize("marked", [True, False])
def test_named_frontend_refuses_missing_marker_or_runtime(tmp_path, native_bins, marked):
    root = installation(tmp_path, native_bins, marked=marked)
    assert subprocess.run([str(root / "TRAKFROM-BX.exe"), "/verify"], timeout=5).returncode != 0


def test_uninstaller_frontend_refuses_unregistered_synthetic_install(tmp_path, native_bins):
    root = installation(tmp_path, native_bins)
    (root / "unins000.exe").write_bytes(b"synthetic uninstaller, never execute")
    assert subprocess.run([str(root / "Uninstall-TRAKFROM-BX.exe"), "/verify"], timeout=5).returncode != 0


def test_named_frontend_starts_isolated_bundled_service_from_other_cwd(tmp_path, native_bins):
    root = installation(tmp_path, native_bins)
    # Reuse the executing Python installation, without invoking product/storage code.
    import sys
    runtime = root / "runtime"
    runtime.mkdir()
    executable = Path(sys.base_prefix)
    python_tag = f"python{sys.version_info.major}{sys.version_info.minor}"
    for name in ("pythonw.exe", python_tag + ".dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        source = executable / name
        if source.exists():
            shutil.copyfile(source, runtime / name)
    library = executable / "Lib"
    embedded_library = executable / (python_tag + ".zip")
    if embedded_library.exists():
        shutil.copyfile(embedded_library, runtime / embedded_library.name)
    else:
        # Use only this interpreter's pure standard library, never site-packages.
        with zipfile.ZipFile(runtime / (python_tag + ".zip"), "w") as archive:
            for path in library.rglob("*.py"):
                relative = path.relative_to(library)
                if relative.parts[0] not in ("site-packages", "test", "ensurepip", "idlelib", "tkinter"):
                    archive.write(path, relative.as_posix())
    (runtime / (python_tag + "._pth")).write_text(python_tag + ".zip\n.\n../app\n", encoding="ascii")
    package = root / "app/rekordbox_usb"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "public_launcher.py").write_text(
        "import sys, os, json\n"
        "with open('native-probe.json', 'w', encoding='utf-8') as f:\n"
        " json.dump({'args':sys.argv[1:],'isolated':sys.flags.isolated,"
        "'no_bytecode':sys.flags.dont_write_bytecode,'cwd':os.getcwd()},f)\n", encoding="utf-8")
    assert subprocess.run([str(root / "TRAKFROM-BX.exe"), "/verify"], timeout=5).returncode == 0
    assert subprocess.run([str(root / "TRAKFROM-BX.exe")], cwd=tmp_path, timeout=5).returncode == 0
    result = root / "native-probe.json"
    deadline = time.monotonic() + 5
    while not result.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    value = json.loads(result.read_text(encoding="utf-8"))
    assert value == {"args": ["service"], "isolated": 1, "no_bytecode": 1, "cwd": str(root)}
