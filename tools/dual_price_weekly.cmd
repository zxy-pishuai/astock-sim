@echo off
rem D4: dual-price weekly report (idempotent recompute). Pure ASCII: codepage-proof.
rem %~dp0 = dir of this .cmd (trailing backslash); ".." lands at project root,
rem avoiding any non-ASCII path literal that cmd.exe would misdecode under GBK.
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
"C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe" tools\dual_price_weekly.py --write >>data\dual_price_weekly.log 2>&1
