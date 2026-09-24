<#
nas-triage.ps1  -  READ ONLY.  Deletes nothing, changes nothing, pushes nothing.
(-Log is the only thing it ever writes, and only where you tell it.)

You opened the NAS and found several complete copies of everything. This tells you
what they are, which one to keep as the live mirror, and what to do next.

    .\nas-triage.ps1                       look at Z:\ , compare against D:\code
    .\nas-triage.ps1 -Size                 also measure each tree (slow over SMB)
    .\nas-triage.ps1 -Depth 4              copies nested deeper than 3 folders
    .\nas-triage.ps1 -Log triage.txt       keep a plain-text copy of this report
    .\nas-triage.ps1 -Root Y:\ -P52 D:\code

Needs: git on PATH. Windows PowerShell 5.1 or newer.
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Root   = 'Z:\',
    [string]$P52    = 'D:\code',
    [int]   $Depth  = 3,
    [int]   $Sample = 40,
    [switch]$Size,
    [string]$Log    = ''
)

Set-StrictMode -Version Latest
# 'Continue', not 'Stop': git writes progress to stderr, which would otherwise throw
# in Windows PowerShell 5.1. Exit codes and output are checked by hand instead.
$ErrorActionPreference = 'Continue'

# NB: PowerShell variable names are case-insensitive, so this must not collide with a counter
$KebabPattern = '^[a-z0-9]+(?:[-.][a-z0-9]+)*$'
$SKIP  = @('.git', 'node_modules', 'target', '.venv', 'venv', '__pycache__', '.mypy_cache',
           '.pytest_cache', 'dist', 'build', '$RECYCLE.BIN', 'System Volume Information',
           '#recycle', '@eaDir', '@Recycle', '.stfolder', '.stversions')

function Say([string]$Text, [string]$Colour = 'Gray') {
    Write-Host $Text -ForegroundColor $Colour
    # Write-Host doesn't go down the pipeline, so -Log is how you keep a copy to send on
    if ($script:Log -ne '') {
        Add-Content -LiteralPath $script:Log -Value $Text -Encoding UTF8 -ErrorAction SilentlyContinue
    }
}

function Test-Repo([string]$Path) {
    # a repo folder: .git as a folder (normal clone) or a file (worktree / submodule)
    return (Test-Path -LiteralPath (Join-Path $Path '.git'))
}

function Find-Repos {
    # repo folders under $Base, without descending into a repo or a cache folder
    param([System.IO.DirectoryInfo]$Base, [int]$MaxDepth)
    $found = New-Object 'System.Collections.Generic.List[System.IO.DirectoryInfo]'
    $stack = New-Object 'System.Collections.Generic.Stack[object]'
    $stack.Push([pscustomobject]@{ Dir = $Base; Depth = 0 })
    while ($stack.Count -gt 0) {
        $node = $stack.Pop()
        if (Test-Repo $node.Dir.FullName) {
            # remember how far down it was: 1 = a direct child of the tree, which is what
            # --nas-base expects. Comparing path strings instead breaks on Z:\ vs Z: and on
            # a mapped drive that resolves to its UNC spelling.
            Add-Member -InputObject $node.Dir -NotePropertyName TriageDepth -NotePropertyValue $node.Depth -Force
            $found.Add($node.Dir)
            continue
        }
        if ($node.Depth -ge $MaxDepth) { continue }
        $kids = @(Get-ChildItem -LiteralPath $node.Dir.FullName -Directory -Force -ErrorAction SilentlyContinue)
        foreach ($k in $kids) {
            if ($SKIP -notcontains $k.Name) {
                $stack.Push([pscustomobject]@{ Dir = $k; Depth = $node.Depth + 1 })
            }
        }
    }
    return $found
}

function Get-NewestCommit {
    # newest commit across every ref, as unix seconds; 0 if the repo has no commits
    param([string]$Repo)
    $d = & git -C $Repo log -1 --all --format=%ct 2>$null
    if ($LASTEXITCODE -ne 0) { return 0 }
    $t = 0
    if ([int64]::TryParse(([string]$d).Trim(), [ref]$t)) { return $t }
    return 0
}

function Get-Origin([string]$Repo) {
    $u = & git -C $Repo config --get remote.origin.url 2>$null
    if ($LASTEXITCODE -ne 0) { return '' }
    return ([string]$u).Trim()
}

function Get-FolderGB([string]$Path) {
    $bytes = 0
    Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue |
        ForEach-Object { $bytes += $_.Length }
    return [math]::Round($bytes / 1GB, 2)
}

# --------------------------------------------------------------------------- checks
if ($Log -ne '') {
    try {
        Set-Content -LiteralPath $Log -Value "nas-triage $(Get-Date -Format 'yyyy-MM-dd HH:mm')  root=$Root  p52=$P52" -Encoding UTF8
    } catch {
        Write-Host "Can't write the log to $Log : $($_.Exception.Message)" -ForegroundColor Red
        exit 2
    }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Say "git isn't on PATH. Install Git for Windows, or open the Git Bash-aware shell." 'Red'
    exit 2
}
if (-not (Test-Path -LiteralPath $Root)) {
    Say "Can't see $Root . Map the NAS first:  net use Z: \\192.168.1.50\GitHub-Repos /persistent:yes" 'Red'
    exit 2
}

# --------------------------------------------------------------------------- P52 side
$p52Names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$p52Count = 0
if (Test-Path -LiteralPath $P52) {
    foreach ($d in @(Get-ChildItem -LiteralPath $P52 -Directory -Force -ErrorAction SilentlyContinue)) {
        if (Test-Repo $d.FullName) { $null = $p52Names.Add($d.Name); $p52Count++ }
    }
}
Say ""
Say "P52  $P52 : $p52Count repos" 'Cyan'
if ($p52Count -eq 0) { Say "  (none found - is -P52 right?)" 'Yellow' }

# --------------------------------------------------------------------------- NAS side
Say "NAS  $Root : looking for copies (up to $Depth folders deep) ..." 'Cyan'

$rootItem = Get-Item -LiteralPath $Root
$topDirs  = @(Get-ChildItem -LiteralPath $Root -Directory -Force -ErrorAction SilentlyContinue |
              Where-Object { $SKIP -notcontains $_.Name })

$trees = New-Object 'System.Collections.Generic.List[object]'
$loose = New-Object 'System.Collections.Generic.List[System.IO.DirectoryInfo]'

foreach ($d in $topDirs) {
    if (Test-Repo $d.FullName) { $loose.Add($d); continue }
    $repos = @(Find-Repos -Base $d -MaxDepth ([math]::Max(1, $Depth - 1)))
    if ($repos.Count -gt 0) {
        $trees.Add([pscustomobject]@{ Name = $d.Name; Dir = $d; Repos = $repos })
    }
}
if ($loose.Count -gt 0) {
    foreach ($l in $loose) { Add-Member -InputObject $l -NotePropertyName TriageDepth -NotePropertyValue 1 -Force }
    $trees.Add([pscustomobject]@{ Name = '(loose at the top level)'; Dir = $rootItem; Repos = $loose })
}

if ($trees.Count -eq 0) {
    Say "No repo copies found under $Root (tried $Depth deep). Try -Depth 5." 'Yellow'
    exit 0
}

# --------------------------------------------------------------------------- measure
$rows = New-Object 'System.Collections.Generic.List[object]'
foreach ($t in $trees) {
    $repos  = @($t.Repos)
    $picked = @($repos | Sort-Object LastWriteTime -Descending | Select-Object -First $Sample)

    $newest = [int64]0
    $withOrigin = 0
    foreach ($r in $picked) {
        $c = Get-NewestCommit $r.FullName
        if ($c -gt $newest) { $newest = $c }
        if ((Get-Origin $r.FullName) -ne '') { $withOrigin++ }
    }

    $kebabCount = 0
    $inP52      = 0
    $direct     = 0
    foreach ($r in $repos) {
        if ($r.Name -cmatch $KebabPattern) { $kebabCount++ }
        if ($p52Names.Contains($r.Name)) { $inP52++ }
        if ($r.PSObject.Properties['TriageDepth'] -and $r.TriageDepth -le 1) { $direct++ }
    }
    # the sync tool treats the direct children of --nas-base as the repos, so a tree that
    # buckets them under an owner folder can't be used as the mirror as it stands
    $layout = 'flat'
    if ($direct -lt $repos.Count) { $layout = "nested($direct/$($repos.Count) flat)" }

    $touched = (@($repos) | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
    $gb = ''
    if ($Size) { $gb = Get-FolderGB $t.Dir.FullName }

    $rows.Add([pscustomobject]@{
        Tree         = $t.Name
        Repos        = $repos.Count
        Layout       = $layout
        AlsoOnP52    = "$inP52 ($([int][math]::Round(100.0 * $inP52 / $repos.Count))%)"
        KebabPct     = [int][math]::Round(100.0 * $kebabCount / $repos.Count)
        NewestFile   = $(if ($null -eq $touched) { '-' } else { $touched.ToString('yyyy-MM-dd') })
        NewestCommit = $(if ($newest -eq 0) { '-' } else { [DateTimeOffset]::FromUnixTimeSeconds($newest).ToString('yyyy-MM-dd') })
        NewestUnix   = $newest
        Origins      = "$withOrigin/$($picked.Count)"
        Usable       = $(if ($picked.Count -eq 0) { 0 } else { [int][math]::Round($repos.Count * $withOrigin / $picked.Count) })
        GB           = $gb
        Path         = $t.Dir.FullName
    })
}

$sorted = @($rows | Sort-Object -Property @{Expression = 'Usable'; Descending = $true},
                                          @{Expression = 'Repos'; Descending = $true},
                                          @{Expression = 'NewestUnix'; Descending = $true})

Say ""
Say "What's on the NAS" 'Cyan'
$cols = @('Tree', 'Repos', 'Layout', 'AlsoOnP52', 'KebabPct', 'NewestFile', 'NewestCommit', 'Origins')
if ($Size) { $cols += 'GB' }
Say (($sorted | Format-Table -Property $cols -AutoSize | Out-String -Width 220).TrimEnd())

Say "  Repos        = repo folders found in that tree"
Say "  Layout       = 'flat' means the repos are direct children, which is what --nas-base expects"
Say "  AlsoOnP52    = how many of them also exist in $P52 by name (name only - a rename reads as 'no')"
Say "  KebabPct     = folder names already in the new all-lowercase-with-dashes style"
Say "  NewestCommit = newest commit in the $Sample most recently touched repos of that tree"
Say "  Origins      = how many of those had a working 'origin' remote"

# --------------------------------------------------------------------------- names D: hasn't got
$onlyNas = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
foreach ($t in $trees) {
    foreach ($r in @($t.Repos)) {
        if (-not $p52Names.Contains($r.Name)) { $null = $onlyNas.Add($r.Name) }
    }
}
Say ""
if ($onlyNas.Count -eq 0) {
    Say "Every repo name on the NAS also exists in $P52 . Nothing here is a lone copy." 'Green'
} else {
    Say "$($onlyNas.Count) repo name(s) on the NAS with no same-named folder in $P52 :" 'Yellow'
    foreach ($n in (@($onlyNas) | Sort-Object | Select-Object -First 40)) { Say "    $n" }
    if ($onlyNas.Count -gt 40) { Say "    ... and $($onlyNas.Count - 40) more" }
    Say "  Most of these are just old names (renamed on GitHub since). 'repo_sync.py dupes' checks by"
    Say "  history, not by name, and keeps anything that really does hold a commit nothing else has." 'Gray'
}

# --------------------------------------------------------------------------- verdict
$flat = @($sorted | Where-Object { $_.Layout -eq 'flat' })
$best = $sorted[0]
if ($flat.Count -gt 0) { $best = $flat[0] }

Say ""
Say "Suggested plan" 'Cyan'
Say "  Keep as the live mirror:  $($best.Path)   ($($best.Repos) repos)"
if ($best.Layout -ne 'flat') {
    Say "  WARNING: every tree buckets its repos under a sub-folder. --nas-base only looks at" 'Yellow'
    Say "  its direct children, so either point --nas-base at one of those sub-folders, or use" 'Yellow'
    Say "  an empty folder and let the sync clone into it fresh." 'Yellow'
}
if ($sorted.Count -gt 1) {
    Say "  Feed the other $($sorted.Count - 1) tree(s) to the duplicate check."
}
Say "  Stale folder names don't matter: repos are matched by origin and history, and the sync"
Say "  renames NAS folders to match the P52. Nothing is identified by folder name."
Say ""
Say "  1) list what holds nothing new - deletes NOTHING:" 'Green'
Say "       py -3 repo_sync.py dupes --root `"$Root`" --p52-base `"$P52`" --nas-base `"$($best.Path)`" --depth $Depth"
Say "  2) read that list, then delete (it asks you to type YES):" 'Green'
Say "       py -3 repo_sync.py dupes --root `"$Root`" --p52-base `"$P52`" --nas-base `"$($best.Path)`" --depth $Depth --apply"
Say "  3) then bring GitHub, the P52 and that mirror into agreement:" 'Green'
Say "       py -3 repo_sync.py sync --nas-base `"$($best.Path)`" --dry-run"
Say ""
Say "  Don't run the old converge.ps1 / repo_convergence.py again - that's what made the copies." 'Yellow'
Say ""
exit 0
