# ============================================================
# W1 | nightly auto-commit (called by scheduled task TianjiGit_Nightly, 23:50)
#   - clear a stale .git/index.lock (only when no git process owns it and it is
#     older than 10 min) so a crashed run cannot wedge every later run
#   - stage all tracked/untracked changes
#   - commit with "auto <timestamp>" message; "nothing to commit" is OK (exit 0)
#   - any real failure (lock held / add error / commit error) logs FAIL and exits
#     1 so the scheduled task Last Result turns non-zero and is visible
#   - append one result line to tmp/git_nightly.log
# ASCII-only content (no Chinese), LF.
# 2026-09-15 hardened during acceptance review: the 2026-09-13 23:51 run left a
# stale 0-byte .git/index.lock behind. From then on `git add -A` failed every
# night while this script still exited 0 -> two nights with no commit, no alarm,
# and ~290 changed files with no restore point.
# ============================================================
Set-Location 'C:\Users\26838\A股模拟盘'
$env:PYTHONIOENCODING = 'utf-8'
# keep git's UTF-8 output readable in the log (was mojibake under GBK console)
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$repo = 'C:\Users\26838\A股模拟盘'
$logFile = Join-Path $repo 'tmp\git_nightly.log'
$lock = Join-Path $repo '.git\index.lock'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

function Write-Log([string]$msg) {
    Add-Content -Path $logFile -Encoding UTF8 -Value ("{0} | {1}" -f $stamp, $msg)
}

function Clip([string]$s) {
    $s = ($s -replace '\s+', ' ').Trim()
    if ($s.Length -gt 220) { $s = $s.Substring(0, 220) }
    return $s
}

# ---- 1. stale lock recovery ----
if (Test-Path -LiteralPath $lock) {
    $gitProcs = @(Get-Process -Name git, git-lfs, git-remote-https -ErrorAction SilentlyContinue)
    $ageMin = ((Get-Date) - (Get-Item -LiteralPath $lock).LastWriteTime).TotalMinutes
    if ($gitProcs.Count -eq 0 -and $ageMin -ge 10) {
        Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
        Write-Log ("LOCKCLEARED stale index.lock removed age_min={0:N1}" -f $ageMin)
    }
    else {
        Write-Log ("FAIL lock_held git_procs={0} age_min={1:N1}" -f $gitProcs.Count, $ageMin)
        exit 1
    }
}

# ---- 2. stage ----
$addOut = Clip (& git add -A 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    Write-Log ("FAIL git_add exit={0} {1}" -f $LASTEXITCODE, $addOut)
    exit 1
}

# ---- 3. commit ("nothing to commit" is a normal, non-failing outcome) ----
$cmsg = "auto " + (Get-Date -Format 'yyyy-MM-dd HH:mm')
$commitOut = Clip (& git commit -m $cmsg --allow-empty-message -q 2>&1 | Out-String)
$cexit = $LASTEXITCODE
$last = Clip (& git log --oneline -1 | Out-String)

if ($cexit -ne 0) {
    if ($commitOut -match 'nothing to commit|working tree clean|no changes added') {
        Write-Log ("OK-CLEAN no changes | {0}" -f $last)
        exit 0
    }
    Write-Log ("FAIL git_commit exit={0} {1}" -f $cexit, $commitOut)
    exit 1
}

Write-Log $last
exit 0
