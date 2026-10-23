@echo off
setlocal
set "ELECTRON_RUN_AS_NODE="
set "ELECTRON_ENABLE_LOGGING=1"
cd /d "%~dp0"
if not exist "node_modules\electron\dist\electron.exe" (
  echo Electron is not installed. Run: npm install
  pause
  exit /b 1
)
echo Launching Listening Studio...
"node_modules\electron\dist\electron.exe" . --enable-logging
echo.
echo Electron exited with code %errorlevel%.
pause
