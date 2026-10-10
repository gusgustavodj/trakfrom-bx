# Install a local public wheel, desktop/Start links and a per-user uninstaller.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WheelPath,
    [string]$PythonExecutable = 'python',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'TRAKFROM-BX'),
    [ValidateSet('Ask', 'Yes', 'No')][string]$RunOnStartup = 'Ask'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$wheel = (Resolve-Path -LiteralPath $WheelPath).Path
if ([IO.Path]::GetExtension($wheel) -ne '.whl' -or
    [IO.Path]::GetFileName($wheel) -notlike 'trakfrom_bx-*.whl') {
    throw 'Select the approved local TRAKFROM-BX public wheel.'
}
. (Join-Path $PSScriptRoot 'windows-install-support.ps1')
$InstallRoot = Get-TrakfromInstallRoot $InstallRoot
Assert-TrakfromTree (Join-Path $InstallRoot 'venv')
Assert-TrakfromRegistration $InstallRoot
foreach ($name in @('uninstall-windows.ps1', 'windows-install-support.ps1', 'install-marker.json')) {
    Assert-TrakfromPath (Join-Path $InstallRoot $name)
}
$marker = Join-Path $InstallRoot 'install-marker.json'
if (Test-Path -LiteralPath $marker) {
    $record = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($record.product -ne 'TRAKFROM-BX' -or $record.install_root -ne $InstallRoot) {
        throw 'Installation marker collision.'
    }
} elseif ((Test-Path -LiteralPath (Join-Path $InstallRoot 'uninstall-windows.ps1')) -or
          (Test-Path -LiteralPath (Join-Path $InstallRoot 'windows-install-support.ps1'))) {
    throw 'Uninstaller filename collision.'
}
if ($RunOnStartup -eq 'Ask') {
    do {
        $answer = Read-Host 'Iniciar TRAKFROM-BX ao entrar no Windows? [s/N]'
    } while ($answer -notmatch '^(s|sim|y|yes|n|nao|no)?$')
    $RunOnStartup = if ($answer -match '^(s|sim|y|yes)$') { 'Yes' } else { 'No' }
}
$shell = New-Object -ComObject WScript.Shell
$links = Get-TrakfromLinks $InstallRoot
foreach ($link in $links) {
    Assert-TrakfromLink $shell $link.Path $link.Target $link.Arguments $link.Description
}
$locks = @(Enter-TrakfromMaintenance)
try {
Invoke-TrakfromPython $PythonExecutable @('-I', '-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)') 'Python 3.11 or newer is required.'
$environment = Join-Path $InstallRoot 'venv'
$python = Join-Path $environment 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Invoke-TrakfromPython $PythonExecutable @('-I', '-m', 'venv', $environment) 'Could not create the per-user Python environment.'
}
Invoke-TrakfromPython $python @('-I', '-m', 'pip', 'install', '--no-index', '--no-deps', '--force-reinstall', $wheel) 'Public package installation failed.'
Invoke-TrakfromPython $python @('-I', '-m', 'rekordbox_usb.public_launcher', 'install-shortcuts') 'Package installed, but desktop shortcut registration failed.'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'uninstall-windows.ps1') -Destination $InstallRoot -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-install-support.ps1') -Destination $InstallRoot -Force
@{ product = 'TRAKFROM-BX'; install_root = $InstallRoot; schema = 1 } |
    ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
New-Item -ItemType Directory -Force -Path (Get-TrakfromPrograms) | Out-Null
foreach ($record in $links) {
    $link = $shell.CreateShortcut($record.Path)
    $link.TargetPath = $record.Target
    $link.Arguments = $record.Arguments
    $link.WorkingDirectory = $InstallRoot
    $link.Description = $record.Description
    $link.Save()
}
$key = Get-TrakfromRegistration
New-Item -Path $key -Force | Out-Null
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$values = @{
    DisplayName = 'TRAKFROM-BX'; DisplayVersion = '1.0'; InstallLocation = $InstallRoot;
    UninstallString = ('"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}"' -f $powershell, (Join-Path $InstallRoot 'uninstall-windows.ps1'));
    DisplayIcon = (Join-Path $environment 'Scripts\pythonw.exe')
}
foreach ($name in $values.Keys) {
    New-ItemProperty -LiteralPath $key -Name $name -Value $values[$name] -PropertyType String -Force | Out-Null
}
foreach ($name in @('NoModify', 'NoRepair')) {
    New-ItemProperty -LiteralPath $key -Name $name -Value 1 -PropertyType DWord -Force | Out-Null
}
$choice = if ($RunOnStartup -eq 'Yes') { 'yes' } else { 'no' }
Invoke-TrakfromPython $python @('-I', '-m', 'rekordbox_usb.public_launcher', 'configure-startup', '--enabled', $choice) 'Application installed, but startup preference could not be saved. Retry the installer.'
Write-Output 'TRAKFROM-BX installed. Use its desktop or Start menu shortcut; wait for the tray before opening Traktor.'
} finally {
    foreach ($mutex in $locks) { $mutex.Dispose() }
}
