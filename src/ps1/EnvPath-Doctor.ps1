# EnvPath-Doctor.ps1
# Safe Windows PATH audit + core repair helper.
#
# This version is OneDrive/Desktop-redirection safe.
#
# Usage:
#   powershell.exe -ExecutionPolicy Bypass -File ".\EnvPath-Doctor.ps1"
#   powershell.exe -ExecutionPolicy Bypass -File ".\EnvPath-Doctor.ps1" -RepairCoreWindowsPaths
#   powershell.exe -ExecutionPolicy Bypass -File ".\EnvPath-Doctor.ps1" -RepairCoreWindowsPaths -AddCommonDevPaths

[CmdletBinding()]
param(
    [switch]$RepairCoreWindowsPaths,
    [switch]$AddCommonDevPaths
)

$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Split-PathVar {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return @()
    }

    return $Value -split ";" |
        ForEach-Object { $_.Trim().Trim('"') } |
        Where-Object { $_ }
}

function Normalize-PathEntry {
    param([string]$Entry)

    if ([string]::IsNullOrWhiteSpace($Entry)) {
        return $null
    }

    try {
        $expanded = [Environment]::ExpandEnvironmentVariables($Entry)
        return [System.IO.Path]::GetFullPath($expanded).TrimEnd("\").ToLowerInvariant()
    } catch {
        return $Entry.TrimEnd("\").ToLowerInvariant()
    }
}

function Ensure-Directory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    [System.IO.Directory]::CreateDirectory($Path) | Out-Null

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Could not create directory: $Path"
    }
}

function Add-EnvPathEntry {
    param(
        [ValidateSet("Machine", "User")]
        [string]$Target,

        [string]$Entry
    )

    $expanded = [Environment]::ExpandEnvironmentVariables($Entry)

    if (-not (Test-Path -LiteralPath $expanded)) {
        Write-Warning "Skipping missing candidate for $Target PATH: $Entry"
        return
    }

    $currentRaw = [Environment]::GetEnvironmentVariable("Path", $Target)
    $parts = Split-PathVar $currentRaw

    $existingNormalized = @{}

    foreach ($part in $parts) {
        $norm = Normalize-PathEntry $part
        if ($norm) {
            $existingNormalized[$norm] = $true
        }
    }

    $entryNorm = Normalize-PathEntry $Entry

    if (-not $existingNormalized.ContainsKey($entryNorm)) {
        $parts += $Entry
        $newValue = ($parts | Where-Object { $_ } | Select-Object -Unique) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $newValue, $Target)
        Write-Host "Added to $Target PATH: $Entry"
    } else {
        Write-Host "Already present in $Target PATH: $Entry"
    }
}

function Show-PathAudit {
    param(
        [ValidateSet("Machine", "User")]
        [string]$Target
    )

    $raw = [Environment]::GetEnvironmentVariable("Path", $Target)
    $parts = Split-PathVar $raw

    Write-Host ""
    Write-Host "=== $Target PATH audit ==="
    Write-Host "Entries: $($parts.Count)"

    $seen = @{}

    foreach ($entry in $parts) {
        $expanded = [Environment]::ExpandEnvironmentVariables($entry)
        $norm = Normalize-PathEntry $entry

        $status = if (Test-Path -LiteralPath $expanded) {
            "OK"
        } else {
            "MISSING"
        }

        $duplicate = ""

        if ($norm -and $seen.ContainsKey($norm)) {
            $duplicate = " DUPLICATE"
        } elseif ($norm) {
            $seen[$norm] = $true
        }

        "{0,-9} {1}{2}" -f "[$status]", $entry, $duplicate
    }
}

function Test-CommandAvailable {
    param([string]$Command)

    $cmd = Get-Command $Command -ErrorAction SilentlyContinue

    if ($cmd) {
        [PSCustomObject]@{
            Command = $Command
            Found   = $true
            Source  = $cmd.Source
        }
    } else {
        [PSCustomObject]@{
            Command = $Command
            Found   = $false
            Source  = ""
        }
    }
}

function Get-SafeScriptBaseDirectory {
    if ($PSScriptRoot -and (Test-Path -LiteralPath $PSScriptRoot -PathType Container)) {
        return $PSScriptRoot
    }

    try {
        $desktopDir = [Environment]::GetFolderPath("DesktopDirectory")
        if ($desktopDir -and (Test-Path -LiteralPath $desktopDir -PathType Container)) {
            return $desktopDir
        }
    } catch {
    }

    return (Get-Location).Path
}

$scriptBaseDir = Get-SafeScriptBaseDirectory
$backupDir = Join-Path $scriptBaseDir "env-path-backups"

Ensure-Directory -Path $backupDir

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupJson = Join-Path $backupDir "env-backup-$timestamp.json"

$backup = [PSCustomObject]@{
    Timestamp             = (Get-Date).ToString("o")
    Computer              = $env:COMPUTERNAME
    User                  = $env:USERNAME
    IsAdmin               = Test-IsAdmin
    ScriptBaseDirectory   = $scriptBaseDir
    BackupDirectory       = $backupDir
    Home                  = $HOME
    CurrentDirectory      = (Get-Location).Path
    RealDesktopDirectory  = [Environment]::GetFolderPath("DesktopDirectory")
    DocumentsDirectory    = [Environment]::GetFolderPath("MyDocuments")
    MachinePath           = [Environment]::GetEnvironmentVariable("Path", "Machine")
    UserPath              = [Environment]::GetEnvironmentVariable("Path", "User")
    ProcessPath           = $env:Path
    ComSpec               = [Environment]::GetEnvironmentVariable("ComSpec", "Machine")
}

$backup |
    ConvertTo-Json -Depth 8 |
    Set-Content -LiteralPath $backupJson -Encoding UTF8

Write-Host "Environment backup written:"
Write-Host "  $backupJson"

$coreWindowsPaths = @(
    "%SystemRoot%\System32",
    "%SystemRoot%",
    "%SystemRoot%\System32\Wbem",
    "%SystemRoot%\System32\WindowsPowerShell\v1.0\",
    "%SystemRoot%\System32\OpenSSH\",
    "%SystemRoot%\SysWOW64\WindowsPowerShell\v1.0\"
)

$commonUserPaths = @(
    "%USERPROFILE%\AppData\Local\Microsoft\WindowsApps",
    "%USERPROFILE%\AppData\Roaming\npm",
    "%LOCALAPPDATA%\Programs\oh-my-posh\bin",
    "%LOCALAPPDATA%\Programs\oh-my-posh"
)

$commonMachineDevPaths = @(
    "%ProgramFiles%\PowerShell\7",
    "%ProgramFiles%\Git\cmd",
    "%ProgramFiles%\Git\bin",
    "%ProgramFiles%\nodejs",
    "C:\msys64\mingw64\bin",
    "C:\msys64\usr\bin",
    "C:\Program Files\CMake\bin",
    "C:\Program Files\Microsoft VS Code\bin"
)

if ($RepairCoreWindowsPaths) {
    if (-not (Test-IsAdmin)) {
        throw "RepairCoreWindowsPaths needs Administrator PowerShell."
    }

    Write-Host ""
    Write-Host "=== Repairing core Windows PATH entries ==="

    foreach ($entry in $coreWindowsPaths) {
        Add-EnvPathEntry -Target Machine -Entry $entry
    }

    $cmdPath = Join-Path $env:SystemRoot "System32\cmd.exe"

    if (Test-Path -LiteralPath $cmdPath) {
        [Environment]::SetEnvironmentVariable("ComSpec", $cmdPath, "Machine")
        Write-Host "ComSpec set to: $cmdPath"
    }
}

if ($AddCommonDevPaths) {
    Write-Host ""
    Write-Host "=== Adding common dev tool PATH entries where present ==="

    foreach ($entry in $commonUserPaths) {
        Add-EnvPathEntry -Target User -Entry $entry
    }

    if (Test-IsAdmin) {
        foreach ($entry in $commonMachineDevPaths) {
            Add-EnvPathEntry -Target Machine -Entry $entry
        }
    } else {
        Write-Warning "Not admin, so machine-level dev paths were not added."
    }
}

$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")

if ($machinePath -and $userPath) {
    $env:Path = "$machinePath;$userPath"
} elseif ($machinePath) {
    $env:Path = $machinePath
} elseif ($userPath) {
    $env:Path = $userPath
}

Show-PathAudit -Target Machine
Show-PathAudit -Target User

Write-Host ""
Write-Host "=== Command availability check ==="

$commands = @(
    "powershell.exe",
    "pwsh.exe",
    "cmd.exe",
    "where.exe",
    "winget.exe",
    "oh-my-posh.exe",
    "git.exe",
    "node.exe",
    "npm.cmd",
    "python.exe",
    "py.exe",
    "code.cmd",
    "gcc.exe",
    "g++.exe",
    "cmake.exe",
    "ninja.exe",
    "cl.exe",
    "msbuild.exe"
)

$commands |
    ForEach-Object { Test-CommandAvailable $_ } |
    Format-Table -AutoSize

Write-Host ""
Write-Host "=== Useful known folders ==="
[PSCustomObject]@{
    Home                 = $HOME
    CurrentDirectory     = (Get-Location).Path
    ScriptBaseDirectory  = $scriptBaseDir
    RealDesktopDirectory = [Environment]::GetFolderPath("DesktopDirectory")
    DocumentsDirectory   = [Environment]::GetFolderPath("MyDocuments")
    BackupDirectory      = $backupDir
} | Format-List

Write-Host ""
Write-Host "Done."
Write-Host "Run without switches for audit-only."
Write-Host "Run with -RepairCoreWindowsPaths for safe Windows core repair."
Write-Host "Run with -AddCommonDevPaths to add common dev paths that already exist."