@echo off
setlocal
cd /d "%~dp0"
curl -s -m 1 http://127.0.0.1:8765/api/config >nul 2>&1
if errorlevel 1 (
  set "PY=%~dp0..\listening-spike\.venv\Scripts\pythonw.exe"
  if not exist "%PY%" set "PY=%~dp0.venv\Scripts\pythonw.exe"
  start "" /min "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --app-dir "%~dp0backend"
  ping -n 7 127.0.0.1 >nul
)
start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app=http://127.0.0.1:8765 --window-size=1320,860 --user-data-dir="%~dp0data\edge-app-profile" --no-first-run
