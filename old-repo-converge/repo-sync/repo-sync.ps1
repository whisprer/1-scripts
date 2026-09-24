<#
repo-sync.ps1  -  run on the P52.

Maps the NAS as Z: if it isn't already, then runs repo_sync.py:
    PLAN  ->  you type YES  ->  APPLY

    .\repo-sync.ps1                         normal run
    .\repo-sync.ps1 -DryRun                 show the plan, change nothing
    .\repo-sync.ps1 -Only whisprer/foo      just one repo (wildcards ok: -Only 'whisprer/*')
    .\repo-sync.ps1 -Dupes                  list old copies that hold nothing new
    .\repo-sync.ps1 -- --allow-mass-delete  anything after -- goes straight to repo_sync.py

If PowerShell refuses to run scripts:
    powershell -ExecutionPolicy Bypass -File .\repo-sync.ps1

First time only - store the NAS login in Windows Credential Manager
(instead of a text file), it asks for the password:
    cmdkey /add:192.168.1.50 /user:wofl /pass
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$DryRun,
    [switch]$Dupes,
    [string[]]$Only,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

Set-StrictMode -Version Latest
# 'Continue', not 'Stop': in Windows PowerShell 5.1 a native command writing to stderr
# (net use, git) would otherwise throw. Exit codes are checked by hand instead.
$ErrorActionPreference = 'Continue'

$NasHost  = '192.168.1.50'
$NasShare = "\\$NasHost\GitHub-Repos"
$Drive    = 'Z:'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'repo_sync.py'
if (-not (Test-Path -LiteralPath $script)) {
    Write-Host "repo_sync.py isn't next to this script ($here)." -ForegroundColor Red
    exit 2
}

# 1) NAS reachable as Z: ?  (an admin PowerShell doesn't see drives mapped in a normal one)
if (-not (Test-Path -LiteralPath "$Drive\")) {
    Write-Host "Mapping $Drive -> $NasShare ..." -ForegroundColor Yellow
    net use $Drive /delete /y 2>&1 | Out-Null          # clears a stale/disconnected mapping
    net use $Drive $NasShare /persistent:yes            # may ask for the NAS login if none is saved
    if (-not (Test-Path -LiteralPath "$Drive\")) {
        Write-Host "Couldn't reach the NAS as $Drive." -ForegroundColor Red
        Write-Host "  Save its login once (it asks for the password):  cmdkey /add:$NasHost /user:wofl /pass" -ForegroundColor Red
        Write-Host "  Or sync only P52 <-> GitHub:  .\repo-sync.ps1 -- --skip-nas" -ForegroundColor Red
        exit 2
    }
    Write-Host "  $Drive mapped." -ForegroundColor Green
}

# 2) Python - standard library only, no venv needed
if (Get-Command py -ErrorAction SilentlyContinue) {
    $exe = 'py'; $pre = @('-3')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $exe = 'python'; $pre = @()
} else {
    Write-Host "Python 3.10+ not found. Install it from python.org (tick 'Add to PATH')." -ForegroundColor Red
    exit 2
}

# 3) Run
if ($Dupes) {
    # looks for copies on the whole NAS share and in the old script's archive folder;
    # the live repos in D:\code and Z:\GitHub-Repos are never touched by this
    $argList = @($script, 'dupes', '--root', "$Drive\")
    $oldArchive = 'D:\code\.repo-convergence-archive'
    if (Test-Path -LiteralPath $oldArchive) { $argList += @('--root', $oldArchive) }
} else {
    $argList = @($script, 'sync')
    if ($DryRun) { $argList += '--dry-run' }
    foreach ($o in @($Only | Where-Object { $_ })) { $argList += @('--only', $o) }
}
if ($Rest) { $argList += $Rest }

& $exe @pre @argList
exit $LASTEXITCODE
