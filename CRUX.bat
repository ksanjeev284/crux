@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo Python 3.9 or newer is required.
  echo https://www.python.org/downloads/
  pause
  exit /b 1
)
python gui.py
if errorlevel 1 pause
