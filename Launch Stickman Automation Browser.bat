@echo off
setlocal
cd /d "%~dp0"

set "URL=http://127.0.0.1:7878"
set "PY=C:\Users\Eliezer\AppData\Local\Programs\Python\Python311\python.exe"
if not exist "%PY%" set "PY=python"

rem If Studio is not up, start it in the background.
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri '%URL%/api/health' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -ne 200) { exit 1 } } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
  start "Stickman Automation API" "%PY%" "%~dp0run_studio.py"
  rem Wait briefly for health
  powershell -NoProfile -Command "for ($i=0; $i -lt 40; $i++) { try { $r = Invoke-WebRequest -Uri '%URL%/api/health' -UseBasicParsing -TimeoutSec 1; if ($r.StatusCode -eq 200) { exit 0 } } catch {} Start-Sleep -Milliseconds 500 }; exit 1"
)

start "" "%URL%"
exit /b 0
