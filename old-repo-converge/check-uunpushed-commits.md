$Root = "D:\code"
Get-ChildItem $Root -Directory -Recurse -Force -ErrorAction SilentlyContinue |
Where-Object { Test-Path (Join-Path $_.FullName ".git") } |
ForEach-Object {
    Push-Location $_.FullName

    $Branch = git branch --show-current 2>$null
    if ($Branch) {
        git fetch --all --quiet 2>$null
        $Unpushed = git log --branches --not --remotes --oneline 2>$null
        if ($Unpushed) {
            Write-Host ""
            Write-Host "=== UNPUSHED COMMITS ===" -ForegroundColor Cyan
            Write-Host $_.FullName
            $Unpushed
        }
    }

    Pop-Location
}
