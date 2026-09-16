@echo off
rem D5: live-vs-backtest weekly attribution (idempotent recompute). Pure ASCII.
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
"C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe" tools\live_vs_backtest_weekly.py --write >>data\live_vs_backtest_weekly.log 2>&1
