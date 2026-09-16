@echo off
rem D2: audit watch daily (silent-failure guard). Pure ASCII: codepage-proof.
rem %~dp0 = dir of this .cmd (trailing backslash); ".." lands at project root,
rem avoiding any non-ASCII path literal that cmd.exe would misdecode under GBK.
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
"C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe" tools\audit_watch.py >>data\audit_watch_cron.log 2>&1
