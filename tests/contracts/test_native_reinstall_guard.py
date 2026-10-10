"""Compile and execute the actual Inno guard against isolated registry fixtures."""
import os
from pathlib import Path
import re
import subprocess
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Native Inno/Windows registry contract')
ROOT = Path(__file__).resolve().parents[2]


def function_source(source, name):
    declaration = re.search(r'(?:function|procedure) ' + re.escape(name), source)
    assert declaration
    start = declaration.start()
    next_function = re.search(r'\n(?:function|procedure) ', source[start + 1:])
    assert next_function
    return source[start:start + 1 + next_function.start()]


@pytest.mark.parametrize('cleanup', [False, True])
@pytest.mark.parametrize('scenario,blocked', [
    ('no-key', False), ('removed-root', False), ('empty-root', False),
    ('installed', True), ('foreign-name', True), ('foreign-path', True),
    ('missing-path', True), ('root-is-file', True), ('nested-record', False),
])
def test_inno_reinstall_distinguishes_removed_legacy_from_active_or_foreign(tmp_path, scenario, blocked, cleanup):
    import winreg
    compiler = Path(os.environ.get('TRAKFROM_BX_INNO_COMPILER', str(
        ROOT / 'outputs/MISSAO_2026-10-09_TRAKFROM_BX_WINDOWS_EXE_INSTALLER/tools/inno/ISCC.exe')))
    if not compiler.is_file():
        pytest.skip('Previously approved Inno compiler not available')
    legacy = tmp_path / 'Legacy local folder'
    if scenario in ('empty-root', 'installed'):
        legacy.mkdir()
    sentinel = legacy / 'install-marker.json'
    if scenario == 'installed':
        sentinel.write_text('synthetic installed application')
    if scenario == 'root-is-file':
        legacy.write_text('foreign file')
    registry_path = 'Software\\TRAKFROM-BX-tests\\' + uuid.uuid4().hex
    source = (ROOT / 'packaging/windows/TRAKFROM-BX.iss').read_text(encoding='utf-8')
    # Only environment boundaries change: registry key and canonical local root.
    guard = '\n'.join(function_source(source, name) for name in
                      ('SafeAncestors', 'DirectoryEmpty', 'LegacyInstallationProblem', 'RemoveStaleLegacyRegistration'))
    guard = guard.replace('Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\TRAKFROM-BX', registry_path)
    guard = guard.replace("ExpandConstant('{localappdata}\\TRAKFROM-BX')", "'" + str(legacy).replace("'", "''") + "'")
    report = tmp_path / 'guard-result.txt'
    harness = tmp_path / 'guard.iss'
    harness.write_text(
        '[Setup]\nAppName=TRAKFROM-BX guard fixture\nAppVersion=1\n'
        'DefaultDirName={tmp}\\unused\nPrivilegesRequired=lowest\n'
        'Uninstallable=no\nCreateAppDir=no\nOutputBaseFilename=guard\n'
        '[Code]\nfunction GetFileAttributesW(Name: String): Cardinal;\n'
        " external 'GetFileAttributesW@kernel32.dll stdcall';\n" + guard +
        '\nfunction InitializeSetup: Boolean;\nvar Problem: String;\nbegin\n'
        ' Problem := LegacyInstallationProblem;\n'
        + (' RemoveStaleLegacyRegistration;\n' if cleanup else '') +
        " SaveStringToFile('" + str(report).replace("'", "''") + "', Problem, False);\n"
        ' Result := False;\nend;\n', encoding='utf-8')
    try:
        if scenario != 'no-key':
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, registry_path, 0,
                                   winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                winreg.SetValueEx(key, 'DisplayName', 0, winreg.REG_SZ,
                                 'Other application' if scenario == 'foreign-name' else 'TRAKFROM-BX')
                if scenario != 'missing-path':
                    winreg.SetValueEx(key, 'InstallLocation', 0, winreg.REG_SZ,
                                     str(tmp_path / 'Foreign') if scenario == 'foreign-path' else str(legacy))
            if scenario == 'nested-record':
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, registry_path + '\\Unexpected', 0,
                                       winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY):
                    pass
        result = subprocess.run([str(compiler), '/O' + str(tmp_path), str(harness)],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        subprocess.run([str(tmp_path / 'guard.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-'],
                       capture_output=True, timeout=15)
        assert report.exists()
        problem = report.read_text(encoding='utf-8-sig')
        assert bool(problem) is blocked, problem
        if scenario == 'installed':
            assert 'Uninstall the previous PowerShell installation first' in problem
            assert sentinel.read_text() == 'synthetic installed application'
        removed = cleanup and scenario in ('removed-root', 'empty-root')
        if removed:
            with pytest.raises(FileNotFoundError):
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path, 0,
                               winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        elif scenario != 'no-key':
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                assert winreg.QueryValueEx(key, 'DisplayName')[0] == (
                    'Other application' if scenario == 'foreign-name' else 'TRAKFROM-BX')
    finally:
        if scenario != 'no-key':
            if scenario == 'nested-record':
                winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, registry_path + '\\Unexpected', winreg.KEY_WOW64_64KEY)
            try:
                winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, registry_path, winreg.KEY_WOW64_64KEY)
            except FileNotFoundError:
                pass


@pytest.mark.parametrize('scenario,action,success', [
    ('update', 0, True), ('reinstall', 1, True), ('uninstall', 2, True),
    ('cancelled', 1, False), ('partial', 1, False), ('foreign-exe', 2, False),
])
def test_native_maintenance_executes_only_registered_uninstall_and_checks_completion(tmp_path, scenario, action, success):
    import winreg
    compiler = Path(os.environ.get('TRAKFROM_BX_INNO_COMPILER', str(
        ROOT / 'outputs/MISSAO_2026-10-09_TRAKFROM_BX_WINDOWS_EXE_INSTALLER/tools/inno/ISCC.exe')))
    if not compiler.is_file():
        pytest.skip('Previously approved Inno compiler not available')
    installation = tmp_path / 'Installed Unicode ç'
    installation.mkdir()
    marker = installation / 'install-marker.ini'
    product_id = '{' + str(uuid.uuid4()) + '}'
    marker.write_text('[installation]\nproduct=TRAKFROM-BX\napp_id=' + product_id +
                      '\nschema=1\ninstallation_id=synthetic\npath=' + str(installation) + '\n', encoding='utf-16')
    preserved = tmp_path / 'user-catalogs-and-preferences.txt'
    preserved.write_text('preserved')
    key_path = 'Software\\TRAKFROM-BX-tests\\' + uuid.uuid4().hex
    receipt = tmp_path / 'uninstaller-ran.txt'
    result_path = tmp_path / 'maintenance-result.txt'
    quote = lambda path: str(path).replace("'", "''")
    fake_name = 'foreign' if scenario == 'foreign-exe' else 'unins000'
    header = ('[Setup]\nAppName=TRAKFROM-BX maintenance fixture\nAppVersion=1\n'
              'DefaultDirName={tmp}\\unused\nPrivilegesRequired=lowest\n'
              'Uninstallable=no\nCreateAppDir=no\nDisableWelcomePage=yes\n'
              'DisableDirPage=yes\nDisableReadyPage=yes\n')
    fake_source = tmp_path / 'uninstaller-fixture.iss'
    fake_source.write_text(header + 'OutputBaseFilename=' + fake_name + '\n[Code]\n'
        'function InitializeSetup: Boolean;\nbegin\n'
        " SaveStringToFile('" + quote(receipt) + "', 'ran', False);\n"
        ' Result := ' + ('False' if scenario == 'cancelled' else 'True') + ';\nend;\n'
        'procedure CurStepChanged(Step: TSetupStep);\nbegin\n'
        + (" if Step = ssPostInstall then begin\n RegDeleteKeyIncludingSubkeys(HKCU, '" + key_path + "');\n"
           " DeleteFile('" + quote(marker) + "');\nend;\n"
           if scenario not in ('cancelled', 'partial') else '') + 'end;\n', encoding='utf-8')
    subprocess.run([str(compiler), '/O' + str(installation), str(fake_source)], check=True,
                   capture_output=True, timeout=30)
    fake_exe = installation / (fake_name + '.exe')
    source = (ROOT / 'packaging/windows/TRAKFROM-BX.iss').read_text(encoding='utf-8')
    functions = '\n'.join(function_source(source, name) for name in
                          ('SafeAncestors', 'SafeTree', 'OwnInstallation', 'RegisteredUninstaller', 'PerformMaintenance'))
    functions = functions.replace('Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{#ProductId}_is1', key_path)
    harness = tmp_path / 'maintenance.iss'
    harness.write_text('#define ProductId "' + product_id + '"\n' + header +
        'OutputBaseFilename=maintenance\n[Code]\n'
        "function GetFileAttributesW(Name: String): Cardinal;\n external 'GetFileAttributesW@kernel32.dll stdcall';\n" +
        functions + '\nfunction InitializeSetup: Boolean;\nvar Ok: Boolean;\nbegin\n'
        ' Ok := PerformMaintenance(' + str(action) + ", '" + quote(installation) + "', True);\n"
        " if Ok then SaveStringToFile('" + quote(result_path) + "', 'ok', False)\n"
        " else SaveStringToFile('" + quote(result_path) + "', 'blocked', False);\n"
        ' Result := False;\nend;\n', encoding='utf-8')
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                               winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, 'UninstallString', 0, winreg.REG_SZ, '"' + str(fake_exe) + '"')
        compile_result = subprocess.run([str(compiler), '/O' + str(tmp_path), str(harness)],
                                        capture_output=True, text=True, timeout=30)
        assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
        subprocess.run([str(tmp_path / 'maintenance.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-'],
                       capture_output=True, timeout=30)
        assert result_path.read_text(encoding='utf-8-sig') == ('ok' if success else 'blocked')
        assert receipt.exists() == (scenario not in ('update', 'foreign-exe'))
        assert marker.exists() == (scenario not in ('reinstall', 'uninstall'))
        assert preserved.read_text() == 'preserved'
    finally:
        try:
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, key_path, winreg.KEY_WOW64_64KEY)
        except FileNotFoundError:
            pass


@pytest.mark.parametrize('cleanup', [False, True])
@pytest.mark.parametrize('scenario,orphan', [
    ('missing-native', True), ('empty-native', True), ('active-native', False),
    ('foreign-module', False), ('extra-args', False), ('root-is-file', False),
    ('missing-legacy', True), ('active-legacy', False), ('changed-value', True),
])
def test_native_startup_migration_checks_command_folder_and_registry_race(tmp_path, scenario, orphan, cleanup):
    import winreg
    compiler = Path(os.environ.get('TRAKFROM_BX_INNO_COMPILER', str(
        ROOT / 'outputs/MISSAO_2026-10-09_TRAKFROM_BX_WINDOWS_EXE_INSTALLER/tools/inno/ISCC.exe')))
    if not compiler.is_file():
        pytest.skip('Previously approved Inno compiler not available')
    folder = tmp_path / 'Removed native folder'
    legacy = tmp_path / 'Local user' / 'TRAKFROM-BX'
    data = tmp_path / 'Local user' / 'RekordboxUSB'
    if scenario in ('empty-native', 'active-native'):
        folder.mkdir()
        if scenario == 'active-native':
            (folder / 'program.txt').write_text('active')
    if scenario == 'root-is-file':
        folder.write_text('foreign file')
    if scenario == 'active-legacy':
        legacy.mkdir(parents=True)
        (legacy / 'program.txt').write_text('active')
    command = '"' + str(folder / 'runtime/pythonw.exe') + '" -I -B -m rekordbox_usb.public_launcher service'
    if scenario == 'foreign-module':
        command = command.replace('rekordbox_usb.public_launcher', 'other.application')
    if scenario == 'extra-args':
        command += ' --unknown'
    if scenario in ('missing-legacy', 'active-legacy'):
        import subprocess as sp
        command = sp.list2cmdline([str(legacy / 'venv/Scripts/pythonw.exe'), '-I', '-m',
                                  'rekordbox_usb.public_launcher', 'service', '--data-root', str(data)])
    key_path = 'Software\\TRAKFROM-BX-tests\\' + uuid.uuid4().hex
    report = tmp_path / 'startup-result.txt'
    source = (ROOT / 'packaging/windows/TRAKFROM-BX.iss').read_text(encoding='utf-8')
    functions = '\n'.join(function_source(source, name) for name in
                          ('SafeAncestors', 'DirectoryEmpty', 'IsOrphanedStartup', 'RemoveValidatedOrphanStartup'))
    quote = lambda value: str(value).replace("'", "''")
    functions = functions.replace("ExpandConstant('{localappdata}\\TRAKFROM-BX')", "'" + quote(legacy) + "'")
    functions = functions.replace("ExpandConstant('{localappdata}\\RekordboxUSB')", "'" + quote(data) + "'")
    harness = tmp_path / 'startup.iss'
    harness.write_text('[Setup]\nAppName=TRAKFROM-BX startup fixture\nAppVersion=1\n'
        'DefaultDirName={tmp}\\unused\nPrivilegesRequired=lowest\nUninstallable=no\n'
        'CreateAppDir=no\nOutputBaseFilename=startup\n[Code]\n'
        "const RunKey = '" + key_path + "';\nvar OrphanedStartup: String;\n"
        "function GetFileAttributesW(Name: String): Cardinal;\n external 'GetFileAttributesW@kernel32.dll stdcall';\n" +
        functions + '\nfunction InitializeSetup: Boolean;\nvar Orphan: Boolean;\nbegin\n'
        " OrphanedStartup := '" + quote(command) + "';\n Orphan := IsOrphanedStartup(OrphanedStartup);\n"
        " if Orphan then SaveStringToFile('" + quote(report) + "', 'yes', False)\n"
        " else SaveStringToFile('" + quote(report) + "', 'no', False);\n"
        + (" RegWriteStringValue(HKCU, RunKey, 'TRAKFROM-BX', 'foreign-changed-command');\n"
           if scenario == 'changed-value' and cleanup else '') +
        (' if Orphan then begin\n try RemoveValidatedOrphanStartup; except end;\nend;\n' if cleanup else '') +
        ' Result := False;\nend;\n', encoding='utf-8')
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                               winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, 'TRAKFROM-BX', 0, winreg.REG_SZ, command)
            winreg.SetValueEx(key, 'Other application', 0, winreg.REG_SZ, 'preserved')
        result = subprocess.run([str(compiler), '/O' + str(tmp_path), str(harness)],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        subprocess.run([str(tmp_path / 'startup.exe'), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-'],
                       capture_output=True, timeout=15)
        assert report.read_text(encoding='utf-8-sig') == ('yes' if orphan else 'no')
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            assert winreg.QueryValueEx(key, 'Other application')[0] == 'preserved'
            if cleanup and orphan and scenario != 'changed-value':
                with pytest.raises(FileNotFoundError):
                    winreg.QueryValueEx(key, 'TRAKFROM-BX')
            else:
                assert winreg.QueryValueEx(key, 'TRAKFROM-BX')[0] == (
                    'foreign-changed-command' if cleanup and scenario == 'changed-value' else command)
    finally:
        winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, key_path, winreg.KEY_WOW64_64KEY)
