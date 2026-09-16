@echo off
rem Risk observation daily report (read-only observer). Pure ASCII: codepage-proof.
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
rem E3: batch-running marker for watchdog (tmp/batch_running\*.flag); removed on exit.
set BFLAG=%~dp0..\tmp\batch_running\risk_daily.flag
if not exist "%~dp0..\tmp\batch_running" mkdir "%~dp0..\tmp\batch_running"
echo started >"%BFLAG%"
"C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe" tools\risk_daily.py >>data\risk_daily_cron.log 2>&1
set RC=%ERRORLEVEL%
echo [%DATE% %TIME%] risk_daily exit=%RC% >>data\risk_daily_cron.log
del "%BFLAG%" 2>nul
exit /b %RC%
