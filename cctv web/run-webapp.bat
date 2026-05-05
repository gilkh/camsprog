@echo off
setlocal
REM Change to this script's directory
cd /d "%~dp0"
echo [Cams Launcher v2] File: %~f0

REM Configure bind host/port (edit if needed)
REM Automatically detect local IP address
for /f "tokens=*" %%i in ('powershell -NoProfile -Command "$ip = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -ne $null } | Select-Object -First 1).IPv4Address[0].IPAddress; if (!$ip) { $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notmatch '^127\.' } | Select-Object -First 1).IPAddress }; if (!$ip) { $ip = '127.0.0.1' }; Write-Host $ip"') do set "LAN_HOST=%%i"
REM Bind to 0.0.0.0 to listen on all network interfaces (more robust).
set "BIND_HOST=0.0.0.0"
REM Load port from PORT.txt and verify it's still available
set "PORT="
if exist "PORT.txt" (
    set /p SAVED_PORT=<PORT.txt
    for /f "tokens=*" %%i in ('powershell -NoProfile -Command "$p='%SAVED_PORT%'; if ($p -match '^\d+$' -and !(Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue)) { Write-Host $p }"') do set "PORT=%%i"
)

if not "%PORT%"=="" goto :PORT_FOUND
for /f "tokens=*" %%i in ('powershell -NoProfile -Command "$listener = [System.Net.Sockets.TcpListener]0; $listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop(); Write-Host $port"') do set "PORT=%%i"
echo %PORT%>PORT.txt
:PORT_FOUND
set "APP_DIR=%~dp0cams-webapp"
set "URL=http://%LAN_HOST%:%PORT%/"

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

REM Ensure dependencies are installed
%PY% -c "import uvicorn, fastapi" >nul 2>&1
if errorlevel 1 (
    echo [Cams] Missing dependencies. Attempting to install...
    %PY% -m pip install -r "%APP_DIR%\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

echo Starting Cams WebApp on %URL%
echo LAN access enabled on port %PORT% (bound to %BIND_HOST%).

REM Stop any stale server already using this port, so old instances do not serve old pages.
powershell -NoProfile -Command "$p='%PORT%'; if ($p -match '^\d+$') { $conns=Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue; if ($conns) { $procIds = $conns | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($procId in $procIds) { try { Stop-Process -Id $procId -Force -ErrorAction Stop; Write-Host ('Stopped stale process PID ' + $procId + ' on port ' + $p) } catch {} } } }"

REM Use cmd /k so the window stays open if the server crashes, allowing you to see the error.
start "Cams WebApp Server" cmd /k "%PY% -m uvicorn app.main:app --host %BIND_HOST% --port %PORT% --app-dir "%APP_DIR%""

powershell -NoProfile -Command "$url='%URL%'; $deadline=(Get-Date).AddSeconds(25); do { try { Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep -Milliseconds 500 } } while ((Get-Date) -lt $deadline)"

REM Open browser (try several methods for reliability across Windows setups).
set "OPENED=0"

powershell -NoProfile -Command "try { Start-Process '%URL%' -ErrorAction Stop; exit 0 } catch { exit 1 }"
if not errorlevel 1 set "OPENED=1"

if "%OPENED%"=="0" (
  start "" "%URL%"
  if not errorlevel 1 set "OPENED=1"
)

if "%OPENED%"=="0" (
  explorer "%URL%"
  if not errorlevel 1 set "OPENED=1"
)

if "%OPENED%"=="0" (
  rundll32 url.dll,FileProtocolHandler "%URL%"
  if not errorlevel 1 set "OPENED=1"
)

if "%OPENED%"=="0" if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" (
  start "" "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" "%URL%"
  if not errorlevel 1 set "OPENED=1"
)

if "%OPENED%"=="0" if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
  start "" "%ProgramFiles%\Google\Chrome\Application\chrome.exe" "%URL%"
  if not errorlevel 1 set "OPENED=1"
)

if "%OPENED%"=="0" echo Could not auto-open browser. Open this URL manually: %URL%

endlocal
