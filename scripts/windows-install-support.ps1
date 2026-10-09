# Authorial helpers shared by the per-user installer and uninstaller.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-TrakfromPython([string]$Executable, [string[]]$Arguments, [string]$FailureMessage) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw $FailureMessage }
}

function Assert-TrakfromPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Redirected installation paths are not allowed.'
            }
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}

function Get-TrakfromInstallRoot([string]$Path) {
    $expected = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'TRAKFROM-BX'))
    if ([IO.Path]::GetFullPath($Path) -ne $expected) {
        throw 'Installation must use the per-user local TRAKFROM-BX directory.'
    }
    Assert-TrakfromPath $expected
    if ((Test-Path -LiteralPath $expected) -and
        -not (Test-Path -LiteralPath $expected -PathType Container)) {
        throw 'Installation root must be a directory.'
    }
    return $expected
}

function Assert-TrakfromTree([string]$Root) {
    Assert-TrakfromPath $Root
    if (Test-Path -LiteralPath $Root -PathType Container) {
        foreach ($child in Get-ChildItem -LiteralPath $Root -Force) {
            Assert-TrakfromPath $child.FullName
            if ($child.PSIsContainer) { Assert-TrakfromTree $child.FullName }
        }
    }
}

function Enter-TrakfromMaintenance {
    $data = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'RekordboxUSB'))
    $locks = @()
    try {
        foreach ($root in @($data, (Join-Path $data 'launcher'))) {
            Assert-TrakfromPath $root
            $sha = [Security.Cryptography.SHA256]::Create()
            try {
                $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))
            } finally { $sha.Dispose() }
            $key = ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
            $created = $false
            $mutex = [Threading.Mutex]::new($false, ('Local\TRAKFROM_BX_' + $key), [ref]$created)
            $locks += $mutex
            if (-not $created) {
                throw 'Close Traktor, choose Encerrar TRAKFROM-BX in the tray, and wait for it to disappear before installing or uninstalling.'
            }
        }
        return $locks
    } catch {
        foreach ($mutex in $locks) { $mutex.Dispose() }
        throw
    }
}

function Get-TrakfromRegistration {
    return 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TRAKFROM-BX'
}

function Assert-TrakfromRegistration([string]$Root) {
    $key = Get-TrakfromRegistration
    if (Test-Path -LiteralPath $key) {
        $record = Get-ItemProperty -LiteralPath $key
        if ($record.DisplayName -ne 'TRAKFROM-BX' -or $record.InstallLocation -ne $Root) {
            throw 'Uninstall registration belongs to another application.'
        }
    }
}

function Get-TrakfromPrograms {
    $programs = [Environment]::GetFolderPath('Programs')
    if (-not $programs) { throw 'Start menu is unavailable.' }
    $folder = Join-Path $programs 'TRAKFROM-BX'
    Assert-TrakfromPath $folder
    return $folder
}

function Get-TrakfromDesktop { return [Environment]::GetFolderPath('DesktopDirectory') }

function Assert-TrakfromLink($Shell, [string]$Path, [string]$Target, [string]$Arguments, [string]$Description) {
    Assert-TrakfromPath $Path
    if (Test-Path -LiteralPath $Path) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'Shortcut path collision.' }
        $link = $Shell.CreateShortcut($Path)
        if ($link.TargetPath -ne $Target -or $link.Arguments -ne $Arguments -or
            $link.Description -ne $Description) { throw 'Shortcut name collision.' }
    }
}

function Get-TrakfromLinks([string]$Root) {
    $pythonw = Join-Path $Root 'venv\Scripts\pythonw.exe'
    $data = Join-Path $env:LOCALAPPDATA 'RekordboxUSB'
    $arguments = '-I -m rekordbox_usb.public_launcher service --data-root '
    # Match subprocess.list2cmdline used by the Python desktop shortcut.
    if ($data -match '\s') { $arguments += '"' + $data + '"' } else { $arguments += $data }
    $programs = Get-TrakfromPrograms
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $uninstall = Join-Path $Root 'uninstall-windows.ps1'
    return @(
        @{ Path = (Join-Path $programs 'TRAKFROM-BX.lnk'); Target = $pythonw;
           Arguments = $arguments; Description = 'TRAKFROM-BX USB service launcher' },
        @{ Path = (Join-Path $programs 'Uninstall TRAKFROM-BX.lnk'); Target = $powershell;
           Arguments = ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $uninstall);
           Description = 'TRAKFROM-BX per-user uninstaller' }
    )
}
