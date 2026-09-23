@echo off
setlocal
cd /d "%~dp0"

rem Load PORT / BUBBLEPOD_PORT from .env when present (default 7878).
set "PORT=7878"
if exist "%~dp0.env" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0.env") do (
    if /I "%%A"=="BUBBLEPOD_PORT" set "PORT=%%B"
    if /I "%%A"=="PORT" set "PORT=%%B"
  )
)
if defined BUBBLEPOD_PORT set "PORT=%BUBBLEPOD_PORT%"
if defined PORT if not "%PORT%"=="" set "PORT=%PORT%"

set "URL=http://127.0.0.1:%PORT%"
set "PY=C:\Users\Eliezer\AppData\Local\Programs\Python\Python311\python.exe"
if not exist "%PY%" set "PY=python"

rem If Studio is not up, start it in the background.
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri '%URL%/api/health' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -ne 200) { exit 1 } } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
  set "BUBBLEPOD_PORT=%PORT%"
  set "PORT=%PORT%"
  start "Stickman Automation API" "%PY%" "%~dp0run_studio.py"
  rem Wait briefly for health
  powershell -NoProfile -Command "for ($i=0; $i -lt 40; $i++) { try { $r = Invoke-WebRequest -Uri '%URL%/api/health' -UseBasicParsing -TimeoutSec 1; if ($r.StatusCode -eq 200) { exit 0 } } catch {} Start-Sleep -Milliseconds 500 }; exit 1"
)

start "" "%URL%"
exit /b 0
