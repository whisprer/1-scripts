#Requires -RunAsAdministrator

$hostsPath = "$env:SystemRoot\System32\drivers\etc\hosts"
$backupPath = "$env:SystemRoot\System32\drivers\etc\hosts.bak_$(Get-Date -Format 'yyyyMMdd_HHmmss')"

$entries = @(
    "0.0.0.0 connect.facebook.net",
    "0.0.0.0 graph.facebook.com",
    "0.0.0.0 fbcdn.net",
    "0.0.0.0 static.xx.fbcdn.net",
    "0.0.0.0 www.facebook.com",
    "0.0.0.0 facebook.com"
)

# Backup first
Copy-Item -Path $hostsPath -Destination $backupPath -Force
Write-Host "[+] Hosts file backed up to: $backupPath" -ForegroundColor Cyan

# Read current hosts content
$currentContent = Get-Content -Path $hostsPath -Raw

# Build list of entries that aren't already present
$toAdd = $entries | Where-Object { $currentContent -notmatch [regex]::Escape($_) }

if ($toAdd.Count -eq 0) {
    Write-Host "[=] All Facebook block entries already present. Nothing to do." -ForegroundColor Yellow
} else {
    # Append a section header + new entries
    $block  = "`n# === FACEBOOK TRACKER BLOCK (added $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) ===`n"
    $block += ($toAdd -join "`n")
    $block += "`n# === END FACEBOOK TRACKER BLOCK ===`n"

    Add-Content -Path $hostsPath -Value $block -Encoding UTF8
    Write-Host "[+] Added $($toAdd.Count) block entries:" -ForegroundColor Green
    $toAdd | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }
}

# Flush DNS cache
Write-Host "[+] Flushing DNS cache..." -ForegroundColor Cyan
ipconfig /flushdns | Out-Null
Write-Host "[+] DNS flushed. All done!" -ForegroundColor Green
