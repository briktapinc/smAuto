@echo off
setlocal
cd /d "%~dp0"

rem Prefer the built Electron app, then fall back to npm start (dev).
if exist "%~dp0dist\win-unpacked\Stickman Automation.exe" (
  start "" "%~dp0dist\win-unpacked\Stickman Automation.exe"
  exit /b 0
)

where npm >nul 2>&1
if %ERRORLEVEL% equ 0 (
  start "Stickman Automation" cmd /c "cd /d ""%~dp0"" && npm start"
  exit /b 0
)

echo Stickman Automation was not found.
echo Build it with:  npm run dist
echo Or install Node and run:  npm start
pause
exit /b 1
