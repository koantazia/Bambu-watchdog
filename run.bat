@echo off
setlocal
cd /d "%~dp0"

set PY=
where python >nul 2>nul && set PY=python
if not defined PY (
  where py >nul 2>nul && set PY=py -3
)
if not defined PY (
  echo Python not found.
  echo Install Python 3.10 or newer from https://www.python.org/downloads/windows/
  echo During setup tick "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)

if not exist a1-watchdog.config.json (
  copy a1-watchdog.config.example.json a1-watchdog.config.json >nul
  echo Created a1-watchdog.config.json - set printer.ip inside it.
)
if not exist a1-watchdog.secrets.json (
  copy a1-watchdog.secrets.example.json a1-watchdog.secrets.json >nul
  echo Created a1-watchdog.secrets.json - set access_code inside it.
)

echo Values starting with PUT_ are placeholders. Run --check first if unsure.
%PY% a1_watchdog.py %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" pause
exit /b %RC%
