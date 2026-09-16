$env:PYTHONIOENCODING = 'utf-8'
$proj = [System.Environment]::GetEnvironmentVariable('K13_PROJ')
if (-not $proj) { Write-Host 'K13_PROJ missing'; exit 1 }
$py = 'python'
$log = Join-Path $proj 'tmp\k13_rapid_scan.log'
& $py @(Join-Path $proj 'tools\rapid_scan.py'), '--rounds', '30', '--interval', '3' *> $log
& $py @(Join-Path $proj 'tmp\k13_accept_duibi.py') *>> $log
Write-Host 'k13 acceptance driver done'
