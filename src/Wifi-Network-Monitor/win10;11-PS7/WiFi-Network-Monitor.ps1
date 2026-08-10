#requires -Version 7.0
[CmdletBinding(DefaultParameterSetName = 'Scan')]
param(
    [Parameter(ParameterSetName = 'Candidate')]
    [switch]$CreateCandidateBaseline,

    [Parameter(ParameterSetName = 'Approve')]
    [switch]$ApproveCandidateBaseline,

    [Parameter(ParameterSetName = 'Scan')]
    [switch]$Scan,

    [string]$InterfaceAlias = 'WiFi',
    [string]$DataDirectory = "$env:ProgramData\WoflNet\WiFiMonitor",
    [ValidateRange(50, 5000)]
    [int]$PingTimeoutMilliseconds = 300,
    [ValidateRange(1, 128)]
    [int]$ThrottleLimit = 32
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-AtomicJson {
    param(
        [Parameter(Mandatory)] [object]$Value,
        [Parameter(Mandatory)] [string]$Path
    )

    $temporaryPath = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporaryPath -Encoding utf8NoBOM
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

function Get-MacVendor {
    param([Parameter(Mandatory)] [string]$MacAddress)

    $prefix = (($MacAddress -replace '-', ':').Substring(0, 8)).ToLowerInvariant()
    if ($script:VendorCache.ContainsKey($prefix)) {
        return $script:VendorCache[$prefix]
    }

    try {
        $vendor = Invoke-RestMethod `
            -Uri "https://api.macvendors.com/$($prefix):" `
            -Method Get `
            -TimeoutSec 10 `
            -ErrorAction Stop
        $vendor = ([string]$vendor).Trim() -replace '[\r\n]+', ' '
        if ([string]::IsNullOrWhiteSpace($vendor)) {
            $vendor = 'Unknown vendor'
        }
    }
    catch {
        $vendor = 'Unknown vendor (lookup unavailable)'
    }

    $script:VendorCache[$prefix] = $vendor
    return $vendor
}

function Get-WiFiNetwork {
    param([Parameter(Mandatory)] [string]$Alias)

    $configuration = Get-NetIPConfiguration -InterfaceAlias $Alias -ErrorAction Stop
    $ipv4 = @($configuration.IPv4Address | Where-Object {
        $_.IPAddress -match '^\d{1,3}(\.\d{1,3}){3}$'
    }) | Select-Object -First 1

    if ($null -eq $ipv4) {
        throw "Interface '$Alias' has no IPv4 address. Connect the Surface to Wi-Fi and try again."
    }

    if ([int]$ipv4.PrefixLength -ne 24) {
        throw "This monitor safely supports /24 Wi-Fi networks. '$Alias' is using /$($ipv4.PrefixLength)."
    }

    $octets = $ipv4.IPAddress.Split('.')
    [pscustomobject]@{
        Address = $ipv4.IPAddress
        Prefix  = "$($octets[0]).$($octets[1]).$($octets[2])"
    }
}

function Invoke-LanDiscovery {
    param(
        [Parameter(Mandatory)] [string]$Alias,
        [Parameter(Mandatory)] [string]$Prefix,
        [Parameter(Mandatory)] [string]$LocalAddress,
        [Parameter(Mandatory)] [int]$TimeoutMilliseconds,
        [Parameter(Mandatory)] [int]$Throttle
    )

    1..254 | ForEach-Object -Parallel {
        & ping.exe -n 1 -w $using:TimeoutMilliseconds "$using:Prefix.$_" *> $null
    } -ThrottleLimit $Throttle

    $neighbors = Get-NetNeighbor -InterfaceAlias $Alias -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object {
            $_.IPAddress -like "$Prefix.*" -and
            $_.IPAddress -ne $LocalAddress -and
            $_.State -notin @('Unreachable', 'Incomplete') -and
            $_.LinkLayerAddress -match '^([0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}$' -and
            $_.LinkLayerAddress -ne 'FF-FF-FF-FF-FF-FF'
        } |
        Sort-Object IPAddress -Unique

    foreach ($neighbor in $neighbors) {
        $mac = $neighbor.LinkLayerAddress.ToUpperInvariant()
        [pscustomobject]@{
            IPAddress  = $neighbor.IPAddress
            MacAddress = $mac
            Vendor     = Get-MacVendor -MacAddress $mac
            State      = [string]$neighbor.State
        }
    }
}

New-Item -ItemType Directory -Path $DataDirectory -Force | Out-Null

$candidatePath = Join-Path $DataDirectory 'candidate-baseline.json'
$baselinePath = Join-Path $DataDirectory 'approved-baseline.json'
$suspiciousLogPath = Join-Path $DataDirectory 'suspicious-devices.log'
$scanLogPath = Join-Path $DataDirectory 'scan-history.log'
$lockPath = Join-Path $DataDirectory 'monitor.lock'

$lockStream = $null
try {
    try {
        $lockStream = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        Write-Host 'Another Wi-Fi scan is already running; this run was skipped.' -ForegroundColor Yellow
        exit 0
    }

    if ($ApproveCandidateBaseline) {
        if (-not (Test-Path -LiteralPath $candidatePath)) {
            throw "No candidate baseline exists. Run this script with -CreateCandidateBaseline first."
        }

        $candidate = Get-Content -LiteralPath $candidatePath -Raw | ConvertFrom-Json
        if (@($candidate.Devices).Count -eq 0) {
            throw 'The candidate baseline contains no devices and will not be approved.'
        }

        Copy-Item -LiteralPath $candidatePath -Destination $baselinePath -Force
        Write-Host "Approved baseline saved to: $baselinePath" -ForegroundColor Green
        Write-Host 'Future scans will flag MAC addresses not contained in this file.'
        exit 0
    }

    $network = Get-WiFiNetwork -Alias $InterfaceAlias
    $script:VendorCache = @{}
    $devices = @(Invoke-LanDiscovery `
        -Alias $InterfaceAlias `
        -Prefix $network.Prefix `
        -LocalAddress $network.Address `
        -TimeoutMilliseconds $PingTimeoutMilliseconds `
        -Throttle $ThrottleLimit)

    $now = Get-Date
    $snapshot = [ordered]@{
        CreatedAt      = $now.ToString('o')
        InterfaceAlias = $InterfaceAlias
        LocalAddress   = $network.Address
        Network        = "$($network.Prefix).0/24"
        Devices        = $devices
    }

    if ($CreateCandidateBaseline) {
        Write-AtomicJson -Value $snapshot -Path $candidatePath
        Write-Host ''
        Write-Host 'Candidate baseline created. These devices are NOT approved yet:' -ForegroundColor Cyan
        $devices | Format-Table IPAddress, MacAddress, Vendor, State -AutoSize
        Write-Host "Inspect this file carefully: $candidatePath" -ForegroundColor Yellow
        Write-Host 'When satisfied, run this script again with -ApproveCandidateBaseline.'
        exit 0
    }

    if (-not (Test-Path -LiteralPath $baselinePath)) {
        throw "No approved baseline exists. Run with -CreateCandidateBaseline, inspect it, then run with -ApproveCandidateBaseline."
    }

    $baseline = Get-Content -LiteralPath $baselinePath -Raw | ConvertFrom-Json
    $approvedDevices = @($baseline.Devices)
    $approvedByMac = @{}
    $approvedByIp = @{}
    foreach ($approved in $approvedDevices) {
        $approvedByMac[[string]$approved.MacAddress] = $approved
        $approvedByIp[[string]$approved.IPAddress] = $approved
    }

    $alerts = [System.Collections.Generic.List[object]]::new()
    foreach ($device in $devices) {
        if (-not $approvedByMac.ContainsKey($device.MacAddress)) {
            $severity = 'WARNING'
            $reason = 'Unapproved MAC address detected'

            if ($approvedByIp.ContainsKey($device.IPAddress)) {
                $severity = 'CRITICAL'
                $expectedMac = [string]$approvedByIp[$device.IPAddress].MacAddress
                $reason = "Approved IP is now using a different MAC; expected $expectedMac"
            }

            $alerts.Add([pscustomobject]@{
                Severity   = $severity
                Reason     = $reason
                IPAddress  = $device.IPAddress
                MacAddress = $device.MacAddress
                Vendor     = $device.Vendor
            })
        }
    }

    $historyLine = '{0} | network={1}.0/24 | devices={2} | alerts={3}' -f `
        $now.ToString('yyyy-MM-dd HH:mm:ss zzz'), $network.Prefix, $devices.Count, $alerts.Count
    Add-Content -LiteralPath $scanLogPath -Value $historyLine -Encoding utf8

    if ($alerts.Count -gt 0) {
        $alertLines = foreach ($alert in $alerts) {
            '{0} | {1} | {2} | IP={3} | MAC={4} | Vendor={5}' -f `
                $now.ToString('yyyy-MM-dd HH:mm:ss zzz'),
                $alert.Severity,
                $alert.Reason,
                $alert.IPAddress,
                $alert.MacAddress,
                $alert.Vendor
        }
        Add-Content -LiteralPath $suspiciousLogPath -Value $alertLines -Encoding utf8
        Write-Host "Suspicious device(s) detected and logged to: $suspiciousLogPath" -ForegroundColor Red
        $alerts | Format-Table Severity, IPAddress, MacAddress, Vendor, Reason -AutoSize
        # A security finding is a successful scan, not a Scheduled Task failure.
        exit 0
    }

    Write-Host "Scan complete: $($devices.Count) approved device(s), no suspicious entries." -ForegroundColor Green
    exit 0
}
finally {
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}
