#requires -Version 5.1
<#
    estate-triage.ps1
    -----------------
    Corruption-vs-breach triage for a Win11 box after a browser credential wipe.

    READ-ONLY. This script only reads local logs and system state. It changes
    NOTHING. Read it before you run it (good instinct for any "security" script).

    It prints a sectioned report as it goes AND saves a timestamped copy to your
    Desktop. Each section gives a plain-English verdict:
        [ OK   ] green  - looks fine
        [ CHECK] amber  - eyeball this yourself, can't auto-decide
        [ FLAG ] red    - counts against "benign", investigate
        [ INFO ] grey   - context / interpretation

    Run from an ADMIN terminal for the full picture (Section 3 needs it).
    Non-admin still works; it just skips the Security-log logon checks cleanly.

        powershell -NoProfile -ExecutionPolicy Bypass -File .\estate-triage.ps1
#>

# ---- setup ---------------------------------------------------------------
$ErrorActionPreference = 'Continue'
$stamp        = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$desktop      = [Environment]::GetFolderPath('Desktop')
$reportPath   = Join-Path $desktop "estate-triage_$stamp.txt"
$script:report   = New-Object System.Collections.Generic.List[string]
$script:flags    = New-Object System.Collections.Generic.List[string]
$script:dpapiOK  = $false

# non-routable ranges we treat as "local" and ignore in network checks
$localRegex = '^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1|fe80|-)'

function Out-Both {
    param([string]$Text, [ConsoleColor]$Color = 'Gray')
    Write-Host $Text -ForegroundColor $Color
    $script:report.Add($Text)
}
function Section {
    param([string]$Title)
    $bar = ('-' * 74)
    Out-Both ""
    Out-Both $bar 'DarkCyan'
    Out-Both "  $Title" 'Cyan'
    Out-Both $bar 'DarkCyan'
}
function Verdict {
    param([string]$Level, [string]$Msg)   # GREEN / CHECK / FLAG / INFO
    switch ($Level) {
        'GREEN' { Out-Both "  [ OK   ] $Msg" 'Green' }
        'CHECK' { Out-Both "  [ CHECK] $Msg" 'Yellow' }
        'FLAG'  { Out-Both "  [ FLAG ] $Msg" 'Red'; $script:flags.Add($Msg) }
        default { Out-Both "  [ INFO ] $Msg" 'Gray' }
    }
}
function Try-Section {
    param([scriptblock]$Body, [string]$Name)
    try { & $Body }
    catch { Out-Both ("  (could not complete '{0}': {1})" -f $Name, $_.Exception.Message) 'DarkYellow' }
}

# ---- am I elevated? ------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
  ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)

# =========================================================================
Section "ESTATE TRIAGE REPORT"
Out-Both ("  Host        : {0}" -f $env:COMPUTERNAME)
Out-Both ("  User        : {0}" -f $env:USERNAME)
Out-Both ("  Generated   : {0}" -f (Get-Date))
Out-Both ("  Admin rights: {0}" -f $(if ($isAdmin) { 'YES' } else { 'NO - Security-log checks limited' }))
try {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    Out-Both ("  Last boot   : {0}" -f $os.LastBootUpTime)
    $up = (Get-Date) - $os.LastBootUpTime
    Out-Both ("  Uptime      : {0}d {1}h {2}m" -f $up.Days, $up.Hours, $up.Minutes)
} catch { }
if (-not $isAdmin) { Verdict 'CHECK' "Not elevated - re-run from an ADMIN terminal for the full picture." }

# =========================================================================
Section "1. REBOOT & SHUTDOWN HISTORY  (did it restart while away, and cleanly?)"
Try-Section -Name 'power events' -Body {
    $evts = $null
    try {
        $evts = Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 1074, 6006, 6008, 41 } `
                             -MaxEvents 40 -ErrorAction Stop
    } catch { }
    if (-not $evts) {
        Verdict 'INFO' "No matching power events in the System log window."
    } else {
        foreach ($e in $evts) {
            $tag = switch ($e.Id) {
                1074 { 'clean restart/shutdown (user/OS/update)' }
                6006 { 'clean shutdown (event log stopped)' }
                6008 { 'UNEXPECTED / unclean shutdown' }
                41   { 'kernel-power: rebooted without clean shutdown' }
                default { '' }
            }
            Out-Both ("    {0}  id={1,-4}  {2}" -f $e.TimeCreated, $e.Id, $tag)
        }
        $unclean = @($evts | Where-Object { $_.Id -in 6008, 41 })
        if ($unclean.Count -gt 0) {
            Verdict 'INFO' ("{0} unclean shutdown(s) - the usual cause of a Chromium/Vivaldi password store wiping mid-write. Leans BENIGN (corruption)." -f $unclean.Count)
        } else {
            Verdict 'INFO' "Only clean restarts seen. Bad-reboot corruption is less likely - keep reading the breach checks."
        }
    }
}

# =========================================================================
Section "2. DPAPI HEALTH  (is Windows' own credential encryption still working?)"
Out-Both "    If DPAPI were broken account-wide, saved Wi-Fi keys and Credential"
Out-Both "    Manager entries would ALSO be unreadable. If they're fine, the damage"
Out-Both "    is Vivaldi-profile-local, not systemic."
Try-Section -Name 'wifi key' -Body {
    $profiles = @( (netsh wlan show profiles) 2>$null |
        Select-String 'All User Profile\s*:\s*(.+)$' |
        ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() } )
    if ($profiles.Count -eq 0) {
        Verdict 'INFO' "No saved Wi-Fi profiles (ethernet box?) - skipping Wi-Fi key test."
    } else {
        $p = $profiles | Select-Object -First 1
        $keyLine = (netsh wlan show profile name="$p" key=clear) 2>$null |
                   Select-String 'Key Content\s*:\s*(.+)$'
        if ($keyLine) {
            Verdict 'GREEN' ("Recovered the saved key for Wi-Fi '{0}' -> DPAPI is healthy." -f $p)
            $script:dpapiOK = $true
        } else {
            Verdict 'CHECK' ("Wi-Fi '{0}' returned no stored key - inconclusive, see Credential Manager below." -f $p)
        }
    }
}
Try-Section -Name 'credential manager' -Body {
    $creds = @( (cmdkey /list) 2>$null | Select-String 'Target:' )
    if ($creds.Count -gt 0) {
        Verdict 'GREEN' ("Credential Manager holds {0} stored credential(s) -> DPAPI reading fine." -f $creds.Count)
        $script:dpapiOK = $true
    } else {
        Verdict 'INFO' "Credential Manager returned no targets (may simply be empty)."
    }
}
if ($script:dpapiOK) {
    Verdict 'GREEN' "System credential encryption works -> the wipe is almost certainly Vivaldi-local. Strong lean to BENIGN."
}

# =========================================================================
Section "3. LOGON ACTIVITY  (any remote or failed logins you didn't make?)"
if (-not $isAdmin) {
    Verdict 'CHECK' "Skipped - needs an elevated terminal to read the Security log."
} else {
    Try-Section -Name 'successful logons' -Body {
        $ok = $null
        try { $ok = Get-WinEvent -FilterHashtable @{ LogName = 'Security'; Id = 4624 } -MaxEvents 200 -ErrorAction Stop } catch { }
        if (-not $ok) {
            Verdict 'INFO' "No 4624 success events in window (logon auditing may be off)."
        } else {
            # 4624 field layout: [5]=user  [8]=LogonType  [18]=IpAddress
            $typed = foreach ($e in $ok) {
                $pp = $e.Properties
                $lt = -1; $usr = '?'; $ip = ''
                if ($pp.Count -gt 8)  { try { $lt = [int]$pp[8].Value } catch { $lt = -1 } }
                if ($pp.Count -gt 5)  { $usr = [string]$pp[5].Value }
                if ($pp.Count -gt 18) { $ip  = [string]$pp[18].Value }
                [pscustomobject]@{ Time = $e.TimeCreated; Type = $lt; User = $usr; Ip = $ip }
            }
            Out-Both "    Logon-type tally (2=console 3=network 7=unlock 10=RemoteDesktop 11=cached):"
            foreach ($g in ($typed | Group-Object Type | Sort-Object Name)) {
                Out-Both ("      type {0,-3} : {1}" -f $g.Name, $g.Count)
            }
            $remote = @($typed | Where-Object { $_.Type -eq 10 })
            if ($remote.Count -gt 0) {
                Verdict 'FLAG' ("{0} RemoteDesktop (type 10) logon(s) present - confirm every one was YOU." -f $remote.Count)
                $remote | Select-Object -First 15 | ForEach-Object {
                    Out-Both ("      RDP  {0}  user={1}  from={2}" -f $_.Time, $_.User, $_.Ip)
                }
            } else {
                Verdict 'GREEN' "No RemoteDesktop (type 10) logons in the window."
            }
            $netExt = @($typed | Where-Object { $_.Type -eq 3 -and $_.Ip -and $_.Ip -notmatch $localRegex })
            if ($netExt.Count -gt 0) {
                Verdict 'CHECK' ("{0} network (type 3) logon(s) from non-local IPs - eyeball below." -f $netExt.Count)
                $netExt | Select-Object -First 15 | ForEach-Object {
                    Out-Both ("      NET  {0}  user={1}  from={2}" -f $_.Time, $_.User, $_.Ip)
                }
            }
        }
    }
    Try-Section -Name 'failed logons' -Body {
        $bad = $null
        try { $bad = Get-WinEvent -FilterHashtable @{ LogName = 'Security'; Id = 4625 } -MaxEvents 200 -ErrorAction Stop } catch { }
        if (-not $bad) {
            Verdict 'GREEN' "No failed-logon (4625) events in the window."
        } else {
            $n = @($bad).Count
            if     ($n -ge 20) { Verdict 'FLAG'  ("{0} failed logons - a lot; possible brute force. Check source below." -f $n) }
            elseif ($n -ge 5)  { Verdict 'CHECK' ("{0} failed logons - above idle noise; glance at the source." -f $n) }
            else               { Verdict 'GREEN' ("{0} failed logon(s) - normal background level." -f $n) }
            # 4625 field layout: [5]=user  [19]=IpAddress
            $bad | Select-Object -First 10 | ForEach-Object {
                $pp = $_.Properties
                $u = '?'; $ip = ''
                if ($pp.Count -gt 5)  { $u  = [string]$pp[5].Value }
                if ($pp.Count -gt 19) { $ip = [string]$pp[19].Value }
                Out-Both ("      fail {0}  user={1}  from={2}" -f $_.TimeCreated, $u, $ip)
            }
        }
    }
}

# =========================================================================
Section "4. LOCAL ACCOUNTS & ADMINISTRATORS  (any account that shouldn't exist?)"
Try-Section -Name 'local users' -Body {
    $users = Get-LocalUser -ErrorAction Stop
    Out-Both "    Enabled local accounts:"
    $users | Where-Object Enabled | Sort-Object Name | ForEach-Object {
        Out-Both ("      {0,-22} lastLogon={1}  pwdSet={2}" -f $_.Name, $_.LastLogon, $_.PasswordLastSet)
    }
    $disabled = @($users | Where-Object { -not $_.Enabled })
    if ($disabled.Count -gt 0) {
        Out-Both ("    Disabled: {0}" -f (($disabled | Select-Object -Expand Name) -join ', '))
    }
    Verdict 'CHECK' "Eyeball the enabled list - any name you don't recognise is a red flag."
}
Try-Section -Name 'administrators' -Body {
    $admins = $null
    try { $admins = Get-LocalGroupMember -Group 'Administrators' -ErrorAction Stop } catch { }
    if ($admins) {
        Out-Both "    Members of Administrators:"
        $admins | ForEach-Object { Out-Both ("      {0}  ({1})" -f $_.Name, $_.ObjectClass) }
    } else {
        Out-Both "    Members of Administrators (via net localgroup fallback):"
        (net localgroup Administrators) 2>$null |
            Select-Object -Skip 6 |
            Where-Object { $_ -and $_ -notmatch 'command completed' } |
            ForEach-Object { Out-Both ("      {0}" -f $_.Trim()) }
    }
    Verdict 'CHECK' "Every admin here should be you or a known service. Anything else = investigate now."
}

# =========================================================================
Section "5. NETWORK LISTENERS & OUTBOUND  (anything phoning home or waiting for a knock?)"
Out-Both "    NOTE: your WireGuard tunnel to the VPS is EXPECTED. WireGuard is UDP so"
Out-Both "    it won't appear in the TCP listener list; an outbound handshake to your"
Out-Both "    SGP1 IP is normal. Look for the UNfamiliar."
Try-Section -Name 'listeners' -Body {
    $listen = Get-NetTCPConnection -State Listen -ErrorAction Stop | Sort-Object LocalPort -Unique
    Out-Both "    TCP ports in LISTEN:"
    foreach ($c in $listen) {
        $pname = '?'
        try { $pname = (Get-Process -Id $c.OwningProcess -ErrorAction Stop).Name } catch { $pname = '?' }
        Out-Both ("      {0,-22}:{1,-6} pid={2,-6} {3}" -f $c.LocalAddress, $c.LocalPort, $c.OwningProcess, $pname)
    }
    if (@($listen | Where-Object LocalPort -eq 3389).Count -gt 0) {
        Verdict 'FLAG' "Port 3389 (RemoteDesktop) is LISTENING. If you never enabled RDP, that's a problem."
    } else {
        Verdict 'GREEN' "RDP (3389) is not listening."
    }
    if (@($listen | Where-Object { $_.LocalPort -in 5985, 5986 }).Count -gt 0) {
        Verdict 'CHECK' "WinRM (5985/5986) is listening - normal on some setups, confirm you actually use it."
    }
}
Try-Section -Name 'outbound' -Body {
    $est = @( Get-NetTCPConnection -State Established -ErrorAction Stop |
        Where-Object { $_.RemoteAddress -notmatch $localRegex -and $_.RemoteAddress -ne '0.0.0.0' } )
    if ($est.Count -eq 0) {
        Verdict 'GREEN' "No established connections to non-local addresses right now."
    } else {
        Out-Both "    Established connections to external IPs:"
        foreach ($c in $est) {
            $pname = '?'
            try { $pname = (Get-Process -Id $c.OwningProcess -ErrorAction Stop).Name } catch { $pname = '?' }
            Out-Both ("      -> {0,-22}:{1,-6} pid={2,-6} {3}" -f $c.RemoteAddress, $c.RemotePort, $c.OwningProcess, $pname)
        }
        Verdict 'CHECK' "Match each to something you trust (browser, WireGuard, updater). Unknown process + external IP = investigate."
    }
}

# =========================================================================
Section "6. PERSISTENCE  (what would relaunch a backdoor after reboot?)"
Try-Section -Name 'run keys' -Body {
    $paths = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run',
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\RunOnce'
    )
    $any = $false
    foreach ($rp in $paths) {
        if (Test-Path $rp) {
            $item = Get-ItemProperty $rp -ErrorAction SilentlyContinue
            if ($item) {
                $props = $item.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' }
                if ($props) {
                    Out-Both ("    {0}" -f $rp)
                    foreach ($pr in $props) { $any = $true; Out-Both ("      {0} = {1}" -f $pr.Name, $pr.Value) }
                }
            }
        }
    }
    if (-not $any) { Verdict 'GREEN' "No autostart entries in the common Run/RunOnce keys." }
    else           { Verdict 'CHECK' "Confirm each autostart above is a program you installed." }
}
Try-Section -Name 'scheduled tasks' -Body {
    $tasks = @( Get-ScheduledTask -ErrorAction Stop |
        Where-Object { $_.State -ne 'Disabled' -and $_.TaskPath -notmatch '^\\Microsoft\\' } )
    if ($tasks.Count -eq 0) {
        Verdict 'GREEN' "No enabled non-Microsoft scheduled tasks."
    } else {
        Out-Both "    Enabled non-Microsoft scheduled tasks:"
        foreach ($t in $tasks) {
            $act = ($t.Actions | ForEach-Object { $_.Execute }) -join ' ; '
            Out-Both ("      {0}{1}   ->  {2}" -f $t.TaskPath, $t.TaskName, $act)
        }
        Verdict 'CHECK' "Any task launching a script/exe from a temp/user/AppData path is suspicious."
    }
}

# =========================================================================
Section "7. VIVALDI PROFILE STATE  (profile corruption, or a full reset?)"
Try-Section -Name 'vivaldi profile' -Body {
    $vivRoot = Join-Path $env:LOCALAPPDATA 'Vivaldi\User Data'
    if (-not (Test-Path $vivRoot)) {
        Verdict 'INFO' "No Vivaldi User Data folder at the default path - skipping."
        return
    }
    $def       = Join-Path $vivRoot 'Default'
    $loginData = Join-Path $def 'Login Data'
    foreach ($f in @($loginData, (Join-Path $def 'Web Data'), (Join-Path $def 'Preferences'))) {
        if (Test-Path $f) {
            $fi = Get-Item $f
            Out-Both ("    {0,-14} size={1,9} bytes  modified={2}" -f $fi.Name, $fi.Length, $fi.LastWriteTime)
        } else {
            Out-Both ("    {0,-14} MISSING" -f (Split-Path $f -Leaf))
        }
    }
    if (Test-Path $loginData) {
        $li = Get-Item $loginData
        if ($li.Length -lt 20KB) {
            Verdict 'INFO' ("'Login Data' is small ({0} bytes) - consistent with an emptied/reset password store (corruption). Leans BENIGN." -f $li.Length)
        } else {
            Verdict 'INFO' ("'Login Data' is {0} bytes - store isn't empty; entries may exist but be undecryptable (keychain/DPAPI mismatch)." -f $li.Length)
        }
    }
    Out-Both "    (the 'modified' timestamps above tell you WHEN the reset happened)"
}

# =========================================================================
Section "SUMMARY"
if ($script:flags.Count -eq 0) {
    Out-Both "  No hard breach-flags were raised by the automated checks." 'Green'
    Out-Both "  Combined with a Vivaldi-only credential wipe, this leans strongly toward" 'Green'
    Out-Both "  PROFILE CORRUPTION (most likely a bad reboot), NOT an intrusion." 'Green'
} else {
    Out-Both ("  {0} item(s) flagged for a closer look:" -f $script:flags.Count) 'Red'
    foreach ($f in $script:flags) { Out-Both ("    - {0}" -f $f) 'Red' }
    Out-Both "  Until these are explained, treat the surviving SSH key as SUSPECT:" 'Red'
    Out-Both "  rotate it (new keypair, replace authorized_keys everywhere, delete old)." 'Red'
}
Out-Both ""
Out-Both "  STILL TO DO BY HAND (this script can't see them):"
Out-Both "    * VPS side:  sudo last -a | head"
Out-Both "                 cat ~/.ssh/authorized_keys /root/.ssh/authorized_keys  (ONLY your key?)"
Out-Both "                 sudo grep Accepted /var/log/auth.log | tail ; sudo ss -tlnp"
Out-Both "    * Run Sysinternals Autoruns for the deep persistence sweep."
Out-Both "    * If Vivaldi Sync was on, see if a cloud copy can restore creds -"
Out-Both "      but don't let an empty local profile overwrite a good cloud one."
Out-Both ""

# ---- write file ----------------------------------------------------------
try {
    $script:report | Out-File -FilePath $reportPath -Encoding UTF8
    Write-Host ""
    Write-Host ("Report saved to: {0}" -f $reportPath) -ForegroundColor Cyan
} catch {
    Write-Host ("Could not write report file: {0}" -f $_.Exception.Message) -ForegroundColor Red
}
