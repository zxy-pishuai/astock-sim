@echo off
rem B4: ml_scores forward ledger daily tick (idempotent). Pure ASCII: codepage-proof.
rem %~dp0 = dir of this .cmd (trailing backslash); ".." lands at project root,
rem avoiding any non-ASCII path literal that cmd.exe would misdecode under GBK.
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
rem E3: batch-running marker for watchdog (tmp/batch_running\*.flag); removed on exit.
set BFLAG=%~dp0..\tmp\batch_running\ml_scores_ledger.flag
if not exist "%~dp0..\tmp\batch_running" mkdir "%~dp0..\tmp\batch_running"
echo started >"%BFLAG%"
"C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe" tools\ml_sidecar\scores_ledger.py >>data\ml_scores_ledger_cron.log 2>&1
del "%BFLAG%" 2>nul
