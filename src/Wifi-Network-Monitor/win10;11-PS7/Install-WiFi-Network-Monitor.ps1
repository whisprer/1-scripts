#requires -Version 7.0
#requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$SourceScript = (Join-Path $PSScriptRoot 'WiFi-Network-Monitor.ps1'),
    [string]$InstallDirectory = "$env:ProgramData\WoflNet\WiFiMonitor",
    [string]$TaskName = 'WoflNet WiFi Security Monitor',
    [string]$InterfaceAlias = 'WiFi'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $SourceScript -PathType Leaf)) {
    throw "Monitor script not found: $SourceScript"
}

$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
$installedScript = Join-Path $InstallDirectory 'WiFi-Network-Monitor.ps1'
$approvedBaseline = Join-Path $InstallDirectory 'approved-baseline.json'

New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null
Copy-Item -LiteralPath $SourceScript -Destination $installedScript -Force

if (-not (Test-Path -LiteralPath $approvedBaseline)) {
    throw @"
No approved baseline exists at:
$approvedBaseline

The monitor was copied but the scheduled task was NOT created.
Run these commands first, inspect the candidate, and approve it:

& '$installedScript' -CreateCandidateBaseline -InterfaceAlias '$InterfaceAlias'
notepad.exe '$InstallDirectory\candidate-baseline.json'
& '$installedScript' -ApproveCandidateBaseline

Then run this installer again as Administrator.
"@
}

$arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Scan -InterfaceAlias "{1}"' -f `
    $installedScript, $InterfaceAlias

$action = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Scans the physical Wi-Fi LAN hourly and logs unapproved MAC addresses.' `
    -Force | Out-Null

Write-Host "Scheduled task installed: $TaskName" -ForegroundColor Green
Write-Host "Monitor: $installedScript"
Write-Host "Suspicious log: $InstallDirectory\suspicious-devices.log"
Write-Host "Scan history: $InstallDirectory\scan-history.log"
