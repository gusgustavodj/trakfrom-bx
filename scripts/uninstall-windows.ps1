# Remove only the identified per-user application. Managed catalogs are preserved.
[CmdletBinding()]
param([switch]$Yes)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'windows-install-support.ps1')
try {
    $root = Get-TrakfromInstallRoot $PSScriptRoot
    $marker = Join-Path $root 'install-marker.json'
    Assert-TrakfromPath $marker
    $record = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($record.product -ne 'TRAKFROM-BX' -or $record.schema -ne 1 -or
        $record.install_root -ne $root) { throw 'Unrecognized installation; nothing removed.' }
    Assert-TrakfromRegistration $root
    $environment = Join-Path $root 'venv'
    # Preflight every recursive target before changing any application registration.
    Assert-TrakfromTree $environment
    $shell = New-Object -ComObject WScript.Shell
    $links = @(Get-TrakfromLinks $root)
    $service = $links[0]
    $desktop = Get-TrakfromDesktop
    if ($desktop) {
        $links += @{ Path = (Join-Path $desktop 'TRAKFROM-BX.lnk'); Target = $service.Target;
                     Arguments = $service.Arguments; Description = $service.Description }
    }
    foreach ($link in $links) {
        Assert-TrakfromLink $shell $link.Path $link.Target $link.Arguments $link.Description
    }
    if (-not $Yes) {
        $answer = Read-Host 'Desinstalar TRAKFROM-BX? Catalogos locais e USB serao preservados. [s/N]'
        if ($answer -notmatch '^(s|sim|y|yes)$') { return }
    }
    $locks = @(Enter-TrakfromMaintenance)
    try {
        $python = Join-Path $environment 'Scripts\python.exe'
        Invoke-TrakfromPython $python @('-I', '-m', 'rekordbox_usb.public_launcher', 'configure-startup', '--enabled', 'no') 'Could not unregister startup; nothing removed.'
        foreach ($link in $links) {
            if (Test-Path -LiteralPath $link.Path) { Remove-Item -LiteralPath $link.Path -Force }
        }
        $key = Get-TrakfromRegistration
        if (Test-Path -LiteralPath $key) { Remove-Item -LiteralPath $key }
        # The absolute recursive target is fixed to this installation's venv.
        $resolved = (Resolve-Path -LiteralPath $environment).Path
        if ($resolved -ne (Join-Path $root 'venv') -or
            -not $resolved.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Invalid removal target.'
        }
        Assert-TrakfromTree $resolved
        Remove-Item -LiteralPath $resolved -Recurse -Force
        foreach ($name in @('install-marker.json', 'windows-install-support.ps1', 'uninstall-windows.ps1')) {
            $path = Join-Path $root $name
            Assert-TrakfromPath $path
            Remove-Item -LiteralPath $path -Force
        }
        $programs = Get-TrakfromPrograms
        if ((Test-Path -LiteralPath $programs) -and -not (Get-ChildItem -LiteralPath $programs -Force)) {
            Remove-Item -LiteralPath $programs
        }
        if (-not (Get-ChildItem -LiteralPath $root -Force)) { Remove-Item -LiteralPath $root }
        Write-Output 'TRAKFROM-BX uninstalled. USB, Traktor and local managed catalogs were preserved.'
    } finally { foreach ($mutex in $locks) { $mutex.Dispose() } }
    if (-not $Yes) { Read-Host 'Press Enter to close this window' | Out-Null }
} catch {
    Write-Host ('Uninstall not completed: ' + $_.Exception.Message)
    if (-not $Yes) { Read-Host 'Press Enter to close this window' | Out-Null }
    exit 1
}
