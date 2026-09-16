@echo off
rem R3-E2 (2026-09-10): audit.jsonl daily rotation (00:05).
rem Calls tools\audit_rotate.py -> app.audit.rotate_daily(keep_days=90).
rem Rollback: schtasks /delete /tn TianjiAuditRotate /f
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
"C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe" tools\audit_rotate.py >>data\audit_rotate_cron.log 2>&1
set RC=%ERRORLEVEL%
echo [%DATE% %TIME%] audit_rotate exit=%RC% >>data\audit_rotate_cron.log
