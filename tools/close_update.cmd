@echo off
rem Close-time incremental update fallback relay (F5 2026-09-02 + A3/A4 2026-09-13).
rem A3: relay mode - TianjiCloseCatchup triggers this every 30 min 15:45-22:00
rem     (replacing the old one-shot 15:45 TianjiCloseUpdate).
rem     Criteria live in app/updater.py --close: run only when today's daily
rem     coverage < 0.95 OR no snapshot of today; no-op (no network) when target
rem     met; after 22:00 no update runs and a deadline WARN is recorded.
rem     Mutual exclusion with main update via data/update.lock (OS file lock).
rem A4: non-trading days exit 0 immediately (no network/DB); SIGINT/SIGBREAK
rem     handler on python side writes an interrupt trace line (0xC000013A case,
rem     09-12 evidence); this cmd's RC echo below is the normal-path exit line.
rem Exit code semantics: 0=OK / no update needed (incl. non-trading skip);
rem     1=update failed; 3221225786=terminated by Ctrl+C / console close.
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
"C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe" -m app.updater --close >>data\close_update_cron.log 2>&1
set RC=%ERRORLEVEL%
if "%RC%"=="3221225786" (
  echo [%DATE% %TIME%] close_update exit=0xC000013A (3221225786, terminated by Ctrl+C/console close) >>data\close_update_cron.log
) else (
  echo [%DATE% %TIME%] close_update exit=%RC% >>data\close_update_cron.log
)
exit /b %RC%
