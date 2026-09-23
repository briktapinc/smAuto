@echo off
setlocal
cd /d "%~dp0"

rem Prefer the built Electron app, then fall back to npm start (dev).
if exist "%~dp0dist\win-unpacked\Bubble Pod.exe" (
  start "" "%~dp0dist\win-unpacked\Bubble Pod.exe"
  exit /b 0
)

where npm >nul 2>&1
if %ERRORLEVEL% equ 0 (
  start "Bubble Pod" cmd /c "cd /d ""%~dp0"" && npm start"
  exit /b 0
)

echo Bubble Pod was not found.
echo Build it with:  npm run dist
echo Or install Node and run:  npm start
pause
exit /b 1
