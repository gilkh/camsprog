@echo off
setlocal
REM Change to this script's directory
cd /d "%~dp0"

REM Configure host/port (edit if needed)
set "HOST=127.0.0.1"
set "PORT=8000"
set "APP_DIR=%~dp0cams-webapp"
set "URL=http://%HOST%:%PORT%/"

REM Prefer project virtualenv if present
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
  set "PY=venv\Scripts\python.exe"
) else if exist "..\.venv\Scripts\python.exe" (
  set "PY=..\.venv\Scripts\python.exe"
) else if exist "..\venv\Scripts\python.exe" (
  set "PY=..\venv\Scripts\python.exe"
) else if exist "%LocalAppData%\Programs\Python\Launcher\py.exe" (
  set "PY=%LocalAppData%\Programs\Python\Launcher\py.exe -3"
) else (
  set "PY=python"
)

echo Starting Cams WebApp on %URL%
start "Cams WebApp Server" %PY% -m uvicorn app.main:app --host %HOST% --port %PORT% --app-dir "%APP_DIR%"

powershell -NoProfile -Command "$url='%URL%'; $deadline=(Get-Date).AddSeconds(20); do { try { Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process $url; exit 0 } catch { Start-Sleep -Milliseconds 500 } } while ((Get-Date) -lt $deadline); Start-Process $url"

endlocal
