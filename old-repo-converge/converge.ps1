# converge.ps1
# Clean, minimal version - fixes the parser error from the previous long header.
# Place this file in the SAME FOLDER as repo_convergence.py and the venv/ folder.
# Then run: powershell -ExecutionPolicy Bypass -File .\converge.ps1
#
# This does your exact workflow in one go:
# - auto cd to the project folder (handles "switch to D:/ root")
# - map Z: persistently
# - activate the venv properly for PowerShell
# - dry run with python (venv's python after activation)
# - ask for confirmation before --apply
# - real run
#
# See the companion README.txt for full explanation, why we use `python` instead of `py -3.14` after activate, and how to customise.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

Write-Host "=== converge starting ===" -ForegroundColor Cyan

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $scriptRoot
Write-Host "Working in: $scriptRoot" -ForegroundColor Green

# Map Z: drive (your net use step)
Write-Host "`n[1] Mapping Z: drive..." -ForegroundColor Yellow
net use Z: \\192.168.1.50\GitHub-Repos /persistent:yes 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "    Z: mapped / already connected" -ForegroundColor Green
} else {
    Write-Host "    Z: note (exit code $LASTEXITCODE) - continuing anyway" -ForegroundColor Yellow
}

# Activate venv (PowerShell way)
$activatePath = Join-Path $scriptRoot "venv\Scripts\Activate.ps1"
if (Test-Path $activatePath) {
    Write-Host "`n[2] Activating venv..." -ForegroundColor Yellow
    & $activatePath
    Write-Host "    Venv active. Python version: $(python --version 2>&1)" -ForegroundColor Green
} else {
    Write-Warning "venv\Scripts\Activate.ps1 not found here. Continuing with whatever 'python' resolves to."
}

# Dry run
Write-Host "`n[3] === DRY RUN ===" -ForegroundColor Cyan
python repo_convergence.py
$dryCode = $LASTEXITCODE
if ($dryCode -ne 0) {
    Write-Host "Dry run exited with code $dryCode - review output above" -ForegroundColor Yellow
} else {
    Write-Host "Dry run completed cleanly." -ForegroundColor Green
}

# Confirmation before apply (safety)
Write-Host "`n[4] Ready to run with --apply ?" -ForegroundColor Yellow
$confirm = Read-Host "Type YES (all caps) to proceed, or anything else to abort"

if ($confirm -ne "YES") {
    Write-Host "Aborted by user. No changes applied. Z: remains mapped." -ForegroundColor Red
    Pop-Location
    exit 0
}

# Real apply run
Write-Host "`n=== EXECUTING --apply ===" -ForegroundColor Red -BackgroundColor Black
python repo_convergence.py --apply
if ($LASTEXITCODE -eq 0) {
    Write-Host "`nApply completed successfully." -ForegroundColor Green
} else {
    Write-Host "`nApply finished with exit code $LASTEXITCODE - check output." -ForegroundColor Yellow
}

Pop-Location
Write-Host "`n=== Done ===" -ForegroundColor Cyan
Write-Host "Z: drive stays mapped persistently. Re-run this script anytime." -ForegroundColor Gray