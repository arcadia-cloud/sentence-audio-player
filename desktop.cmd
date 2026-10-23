@echo off
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%~dp0..\listening-spike\.venv\Scripts\pythonw.exe"
if not exist "%PY%" (
  echo Python environment not found. Run start.ps1 first.
  pause
  exit /b 1
)
start "" "%PY%" "%~dp0desktop.py"
