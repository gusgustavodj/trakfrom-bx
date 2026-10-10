"""Authorial offline assembly; inputs must already be obtained and verified."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import zipfile

PYTHON_SHA256 = "97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297"
RUNTIME_FILES = set("""python.exe pythonw.exe python313.dll python3.dll vcruntime140.dll
vcruntime140_1.dll LICENSE.txt pyexpat.pyd select.pyd unicodedata.pyd winsound.pyd
_asyncio.pyd _bz2.pyd _ctypes.pyd _decimal.pyd _elementtree.pyd _hashlib.pyd _lzma.pyd
_multiprocessing.pyd _overlapped.pyd _queue.pyd _socket.pyd _sqlite3.pyd _ssl.pyd
_uuid.pyd _wmi.pyd _zoneinfo.pyd libcrypto-3.dll libffi-8.dll libssl-3.dll sqlite3.dll
python313.zip python313._pth python.cat""".split())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compile_frontends(stage, compiler, sdk):
    """Compile own GUI entrypoints against existing Windows SDK import libraries."""
    source = Path(__file__).with_name("native_frontend.c")
    versions = sorted(path.name for path in (sdk / "Include").iterdir()
                      if (path / "um/windows.h").is_file())
    assert versions and compiler.is_file() and source.is_file()
    version = versions[-1]
    include = sdk / "Include" / version
    libraries = sdk / "Lib" / version / "um/x64"
    tool_include = compiler.parents[3] / "include"
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(sdk / "bin" / version / "x64"),
                                         str(compiler.parent), environment.get("PATH", "")))
    for name, uninstall in (("TRAKFROM-BX.exe", 0), ("Uninstall-TRAKFROM-BX.exe", 1)):
        subprocess.run([
            str(compiler), "/nologo", "/O1", "/GS-", "/Zl", "/W4", "/WX",
            "/DUNICODE", "/D_UNICODE", f"/DUNINSTALL_FRONTEND={uninstall}",
            "/I" + str(include / "um"), "/I" + str(include / "shared"),
            "/I" + str(include / "ucrt"), "/I" + str(tool_include),
            "/Fo" + str(stage.parent / (stage.name + "-" + name + ".obj")), str(source),
            "/link", "/NODEFAULTLIB", "/ENTRY:entry", "/SUBSYSTEM:WINDOWS", "/MACHINE:X64",
            "/DYNAMICBASE", "/NXCOMPAT", "/HIGHENTROPYVA", "/MANIFEST:EMBED",
            "/MANIFESTUAC:level='asInvoker' uiAccess='false'",
            "/LIBPATH:" + str(libraries), "kernel32.lib", "user32.lib", "advapi32.lib", "shell32.lib",
            "/OUT:" + str(stage / name),
        ], check=True, env=environment, cwd=compiler.parent)


def assemble(candidate, wheel, archive, licenses, stage, native_compiler, windows_sdk):
    assert not stage.exists(), "Use a new staging directory; never delete an old one"
    assert sha(archive) == PYTHON_SHA256
    allowed = set((candidate / "PUBLIC_FILES.txt").read_text(encoding="utf-8").splitlines())
    source = {name[4:] for name in allowed if name.startswith("src/rekordbox_usb/")}
    with zipfile.ZipFile(wheel) as package:
        app = {name: package.read(name) for name in package.namelist() if name.startswith("rekordbox_usb/")}
    assert set(app) == source
    assert all(data == (candidate / "src" / name).read_bytes() for name, data in app.items())
    stage.mkdir(parents=True)
    with zipfile.ZipFile(archive) as embedded:
        assert set(embedded.namelist()) == RUNTIME_FILES
        for name in sorted(RUNTIME_FILES):
            assert PurePosixPath(name).name == name
            target = stage / "runtime" / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(embedded.read(name))
    for name, data in app.items():
        target = stage / "app" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (stage / "runtime/python313._pth").write_text("python313.zip\n.\n../app\n", encoding="ascii")
    (stage / "runtime/trakfrom-bx-runtime.json").write_text(
        json.dumps({"product": "TRAKFROM-BX", "schema": 1, "python": "3.13.16"}) + "\n", encoding="utf-8")
    shutil.copyfile(candidate / "LICENSE", stage / "LICENSE.txt")
    for path in sorted(licenses.glob("*.txt")):
        target = stage / "licenses" / path.name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, target)
    (stage / "THIRD_PARTY_NOTICES.txt").write_text(
        "TRAKFROM-BX: MIT; see LICENSE.txt.\n"
        "Official CPython Embedded 3.13.16 x64 and its bundled components: see runtime/LICENSE.txt and licenses/.\n"
        "Python source: https://www.python.org/downloads/release/python-31316/\n"
        "SQLite is dedicated to the public domain: https://www.sqlite.org/copyright.html\n"
        "Inno Setup installer engine: copyright Jordan Russell and Martijn Laan; see licenses/INNO_LICENSE.txt.\n"
        "No third-party application runtime packages are installed.\n", encoding="utf-8")
    compile_frontends(stage, native_compiler, windows_sdk)
    entries = {}
    for path in sorted(stage.rglob("*")):
        assert not path.is_symlink()
        if path.is_file():
            name = path.relative_to(stage).as_posix()
            assert not any(part in name for part in ("__pycache__", ".venv", ".git", "tests/", "outputs/"))
            if name.startswith("app/"):
                data = path.read_bytes().lower()
                assert b"c:\\traktorbox" not in data and b"c:\\users\\" not in data
            entries[name] = sha(path)
    return entries


def compile_setup(stage, manifest, compiler, output):
    assert {path.relative_to(stage).as_posix(): sha(path) for path in stage.rglob("*") if path.is_file()} == manifest
    output.mkdir(parents=True, exist_ok=True)
    include = stage.parent / (stage.name + "-payload.iss")
    lines = []
    for name in sorted(manifest):
        source = str(stage / name)
        destination = "{app}" + ("\\" + str(Path(name).parent) if Path(name).parent != Path(".") else "")
        assert '"' not in source and '"' not in destination
        lines.append(f'Source: "{source}"; DestDir: "{destination}"; Flags: ignoreversion')
    include.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return subprocess.call([str(compiler), "/DStageDir=" + str(stage), "/DOutputPath=" + str(output),
                            "/DPayloadInclude=" + str(include), str(Path(__file__).with_name("TRAKFROM-BX.iss"))])


def main():
    parser = argparse.ArgumentParser()
    for name in ("candidate", "wheel", "python-zip", "licenses", "stage", "compiler", "output",
                 "native-compiler", "windows-sdk"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--assemble-only", action="store_true")
    args = parser.parse_args()
    manifest = assemble(args.candidate, args.wheel, args.python_zip, args.licenses, args.stage,
                        args.native_compiler, args.windows_sdk)
    (args.stage.parent / (args.stage.name + "-manifest.json")).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not args.assemble_only:
        return compile_setup(args.stage, manifest, args.compiler, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
