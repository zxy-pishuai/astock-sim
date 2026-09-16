$env:PYTHONIOENCODING = 'utf-8'
$proj = [System.Environment]::GetEnvironmentVariable('K13_PROJ')
if (-not $proj) { Write-Host 'K13_PROJ missing'; exit 1 }
$log = Join-Path $proj 'tmp\k14_eps_collect.log'
& python @(Join-Path $proj 'tools\k14_eps_collect.py'), '--full' *> $log
& python @(Join-Path $proj 'tools\k14_eps_qc.py') *>> $log
Write-Host 'k14 daily snapshot done'
