#Requires -Version 5.1
<#
rotate-passwords.ps1 - change your sudo (wofl) password and root's password on all four servers.

Run from PowerShell on your PC, in the folder holding this file and pw-rotate-remote.sh:
    .\rotate-passwords.ps1
    .\rotate-passwords.ps1 -Servers nyc1          # just one (or a few) servers

Nothing is typed into this file. It asks for the passwords while running (hidden as you type),
keeps them only in memory, and sends them to each server through the SSH connection's input,
never on a command line. On each server the helper checks your current password, changes both,
proves the new ones are stored and that sudo accepts yours, and puts your old one back if yours
didn't take - so you can't lock yourself out of sudo.

New passwords: by default it makes long random ones, different for every server and account
(best practice). It shows each server's new pair as soon as that server is done - put them in
your password manager straight away. They are never written to disk.
#>
[CmdletBinding()]
param([string[]]$Servers = @('lon1', 'syd1', 'nyc1', 'sgp1'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$helper = Join-Path $PSScriptRoot 'pw-rotate-remote.sh'
if (-not (Test-Path -LiteralPath $helper)) { throw "pw-rotate-remote.sh must be in the same folder as this script ($PSScriptRoot)." }
foreach ($c in @('ssh', 'scp')) { if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { throw "Missing command: $c" } }

# The helper must have Linux line endings, whatever an editor may have done to it.
$text = [IO.File]::ReadAllText($helper)
$upload = $helper
if ($text.Contains("`r`n")) {
    $upload = Join-Path ([IO.Path]::GetTempPath()) 'pw-rotate-remote.sh'
    [IO.File]::WriteAllText($upload, $text.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
}

function Read-Secret([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Test-Usable([string]$Pw, [int]$Min) {
    # printable ASCII only (survives every keyboard layout and the trip over SSH), long enough
    if ($Pw.Length -lt $Min) { return "shorter than $Min characters" }
    foreach ($ch in $Pw.ToCharArray()) { if ([int]$ch -lt 32 -or [int]$ch -gt 126) { return 'has a character outside plain ASCII (or a tab)' } }
    return $null
}

function Read-NewSecret([string]$What) {
    while ($true) {
        $a = Read-Secret "New password for $What (16+ characters)"
        $problem = Test-Usable $a 16
        if ($problem) { Write-Host "  That one $problem - try again." -ForegroundColor Yellow; continue }
        $b = Read-Secret "Type it again"
        if ($a -cne $b) { Write-Host '  They did not match - try again.' -ForegroundColor Yellow; continue }
        return $a
    }
}

function New-RandomPassword([int]$Length = 28) {
    # no colon, quotes, spaces or backslash; no look-alikes (0/O, 1/l/I)
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_.+=@%#'
    $limit = 256 - (256 % $chars.Length)   # avoid bias
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $one = New-Object byte[] 1
    $sb = New-Object Text.StringBuilder
    while ($sb.Length -lt $Length) {
        $rng.GetBytes($one)
        if ($one[0] -lt $limit) { [void]$sb.Append($chars[$one[0] % $chars.Length]) }
    }
    $rng.Dispose()
    return $sb.ToString()
}

Write-Host "`nRotating the wofl (sudo) and root passwords on: $($Servers -join ', ')`n" -ForegroundColor Cyan

# --- current passwords
$current = @{}
$same = Read-Host 'Is your CURRENT wofl password the same on all of them? (y/N)'
if ($same -match '^(y|yes)$') {
    $pw = Read-Secret 'Current wofl password'
    foreach ($h in $Servers) { $current[$h] = $pw }
} else {
    foreach ($h in $Servers) { $current[$h] = Read-Secret "Current wofl password on $h" }
}

# --- new passwords
$new = @{}
$gen = Read-Host 'Make new RANDOM passwords, different for every server and account? (Y/n - recommended)'
if ($gen -match '^(n|no)$') {
    Write-Host '  You will type one new wofl password and one new root password, used on every server.' -ForegroundColor Yellow
    $w = Read-NewSecret 'wofl (all servers)'
    $r = Read-NewSecret 'root (all servers)'
    foreach ($h in $Servers) { $new[$h] = @{ wofl = $w; root = $r } }
} else {
    foreach ($h in $Servers) { $new[$h] = @{ wofl = (New-RandomPassword); root = (New-RandomPassword) } }
}

Write-Host "`nThis will, one server at a time: copy the helper up, change both passwords, check them, remove the helper."
Write-Host 'New passwords are shown per server as soon as it is done. Have your password manager open.'
if ((Read-Host 'Type yes to start') -ne 'yes') { Write-Host 'Nothing changed.'; return }

# From here on, a message from ssh on its error output must never stop the run halfway through
# (Windows PowerShell 5.1 turns those into terminating errors under 'Stop').
$ErrorActionPreference = 'Continue'
$results = @()
foreach ($h in $Servers) {
    Write-Host "`n== $h" -ForegroundColor Cyan
    & scp -q -o BatchMode=yes $upload "${h}:pw-rotate-remote.sh"
    if ($LASTEXITCODE -ne 0) { Write-Host "  couldn't copy the helper to $h - skipped, nothing changed there" -ForegroundColor Red; $results += [pscustomobject]@{ Server = $h; Result = 'skipped (copy failed)' }; continue }

    $payload = "$($current[$h])`n$($new[$h].wofl)`n$($new[$h].root)"
    $out = ($payload | & ssh -o BatchMode=yes $h 'bash ~/pw-rotate-remote.sh' 2>&1 | Out-String)
    $code = $LASTEXITCODE
    $payload = $null
    & ssh -o BatchMode=yes $h 'rm -f ~/pw-rotate-remote.sh' 2>&1 | Out-Null
    Write-Host $out.TrimEnd()

    $changed = $out -match 'current password OK' -and $out -notmatch 'nothing changed'
    if ($changed -and $gen -notmatch '^(n|no)$') {
        $colour = 'Green'; if ($code -ne 0) { $colour = 'Yellow' }
        Write-Host "  SAVE NOW -> $h  wofl: $($new[$h].wofl)" -ForegroundColor $colour
        Write-Host "  SAVE NOW -> $h  root: $($new[$h].root)" -ForegroundColor $colour
        if ($out -match 'OLD one is back') { Write-Host "  (on $h your wofl password is still the OLD one; root's is the new one above)" -ForegroundColor Yellow }
    }
    $status = 'FAILED - see above'
    if ($code -eq 0) { $status = 'changed + verified' }
    elseif ($code -eq 4) { $status = 'wofl changed; root needs a rerun' }
    elseif ($out -match 'nothing changed') { $status = 'unchanged (see above)' }
    $results += [pscustomobject]@{ Server = $h; Result = $status }
}

Write-Host "`n== Summary" -ForegroundColor Cyan
$results | Format-Table -AutoSize | Out-String | Write-Host
Write-Host 'Before closing this window: check every new password is saved in your password manager.' -ForegroundColor Yellow
Write-Host 'Then test one: ssh into a server and run  sudo -k; sudo -v  (it asks for the NEW password).'

# drop the secrets from this session
$current = $null; $new = $null; $pw = $null; $w = $null; $r = $null
if ($upload -ne $helper) { Remove-Item -LiteralPath $upload -Force -ErrorAction SilentlyContinue }
