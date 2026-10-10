# Synthetic contract harness: real PowerShell/COM links and mutexes, mocked Python
# and uninstall registration. Removal is recorded, never executed by this harness.
param([string]$Source, [string]$Work, [string]$Choice = 's', [string]$Scenario = 'normal')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:LOCALAPPDATA = Join-Path $Work 'Local User'
$script:programs = Join-Path $Work 'Programs'
$script:desktop = Join-Path $Work 'Desktop'
$script:registration = Join-Path $Work 'Registration'
$script:calls = @()
$script:removals = @()
$script:prompts = @()
$script:properties = @{}
foreach ($path in @($env:LOCALAPPDATA, $script:desktop)) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}
. (Join-Path $Source 'scripts\windows-install-support.ps1')
function Get-TrakfromPrograms { return $script:programs }
function Get-TrakfromDesktop { return $script:desktop }
function Get-TrakfromRegistration { return $script:registration }
function Read-Host([string]$Prompt) { $script:prompts += $Prompt; return $Choice }
function New-ItemProperty($LiteralPath, $Name, $Value, $PropertyType, [switch]$Force) {
    if ($LiteralPath -ne $script:registration) { throw 'Unexpected registry effect.' }
    $script:properties[$Name] = $Value
}
function Get-ItemProperty($LiteralPath) {
    if ($LiteralPath -ne $script:registration) { throw 'Unexpected registry read.' }
    return [pscustomobject]$script:properties
}
function Remove-Item($LiteralPath, [switch]$Force, [switch]$Recurse) {
    $script:removals += @{path = $LiteralPath; recursive = [bool]$Recurse}
}
function Invoke-TrakfromPython([string]$Executable, [string[]]$Arguments, [string]$FailureMessage) {
    $script:calls += @{executable = $Executable; arguments = $Arguments}
    if ($Arguments -contains 'venv') {
        $directory = Join-Path $Arguments[-1] 'Scripts'
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
        foreach ($name in @('python.exe', 'pythonw.exe')) {
            New-Item -ItemType File -Path (Join-Path $directory $name) | Out-Null
        }
    }
    if ($Arguments -contains 'install-shortcuts') {
        $root = Join-Path $env:LOCALAPPDATA 'TRAKFROM-BX'
        $record = @(Get-TrakfromLinks $root)[0]
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut((Join-Path $script:desktop 'TRAKFROM-BX.lnk'))
        $link.TargetPath = $record.Target
        $link.Arguments = $record.Arguments
        $link.Description = $record.Description
        $link.Save()
    }
}
# Execute the unmodified installer bodies with only external boundaries replaced.
# Dot-sourcing is removed so mocks stay in place; each script's PSScriptRoot is
# supplied by the wrapper scope, as it would be when run from its saved location.
function Invoke-FixtureScript([string]$Path, [hashtable]$Parameters) {
    $PSScriptRoot = Split-Path -Parent $Path
    $text = Get-Content -LiteralPath $Path -Raw
    $text = $text.Replace(". (Join-Path `$PSScriptRoot 'windows-install-support.ps1')", '# Helpers already loaded by harness')
    $text = $text.Replace('$PSScriptRoot', ("'" + $PSScriptRoot.Replace("'", "''") + "'"))
    & ([scriptblock]::Create($text)) @Parameters
}
$wheel = Join-Path $Work 'trakfrom_bx-1.0-py3-none-any.whl'
New-Item -ItemType File -Path $wheel | Out-Null
$root = Join-Path $env:LOCALAPPDATA 'TRAKFROM-BX'
$blocked = $false
$failure = ''
$held = @()
try {
    if ($Scenario -eq 'running') { $held = @(Enter-TrakfromMaintenance) }
    if ($Scenario -eq 'foreign-link') {
        New-Item -ItemType Directory -Path $script:programs | Out-Null
        $shell = New-Object -ComObject WScript.Shell
        $foreign = $shell.CreateShortcut((Join-Path $script:programs 'TRAKFROM-BX.lnk'))
        $foreign.TargetPath = Join-Path $Work 'foreign.exe'
        $foreign.Description = 'Foreign link'
        $foreign.Save()
    }
    Invoke-FixtureScript (Join-Path $Source 'scripts\install-windows.ps1') @{WheelPath = $wheel}
} catch { $blocked = $true; $failure = $_.ToString() + ' at ' + $_.ScriptStackTrace }
finally { foreach ($mutex in $held) { $mutex.Dispose() } }
if ($Scenario -ne 'normal') {
    @{blocked = $blocked; failure = $failure; calls = $script:calls; removals = $script:removals} | ConvertTo-Json -Depth 8
    exit 0
}
if ($blocked) { throw ('Synthetic install unexpectedly failed: ' + $failure) }
$shell = New-Object -ComObject WScript.Shell
$links = @(Get-TrakfromLinks $root)
$saved = @()
foreach ($record in $links) {
    $link = $shell.CreateShortcut($record.Path)
    $saved += @{path = $record.Path; target = $link.TargetPath; arguments = $link.Arguments;
                description = $link.Description}
}
# Upgrade keeps the existing preference contract and replaces only owned links.
Invoke-FixtureScript (Join-Path $Source 'scripts\install-windows.ps1') @{WheelPath = $wheel; RunOnStartup = 'No'}
$data = Join-Path $env:LOCALAPPDATA 'RekordboxUSB'
New-Item -ItemType Directory -Path $data | Out-Null
$sentinel = Join-Path $data 'managed-catalog-sentinel.txt'
Set-Content -LiteralPath $sentinel -Value 'preserve'
Invoke-FixtureScript (Join-Path $root 'uninstall-windows.ps1') @{Yes = $true}
@{blocked = $false; prompts = $script:prompts; calls = $script:calls;
  registration = $script:properties; links = $saved; removals = $script:removals;
  preserved = (Get-Content -LiteralPath $sentinel -Raw).Trim(); install_root = $root;
  data_root = $data} | ConvertTo-Json -Depth 8
