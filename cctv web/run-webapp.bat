@echo off
setlocal
REM Change to this script's directory
cd /d "%~dp0"

REM Configure host/port (edit if needed)
set "HOST=0.0.0.0"
set "PORT=8000"

REM Prefer project virtualenv if present
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
  set "PY=venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo Starting Cams WebApp on %HOST%:%PORT%
"%PY%" -m uvicorn cams-webapp.app.main:app --host %HOST% --port %PORT%

endlocal
