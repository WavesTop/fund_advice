@echo off
setlocal
cd /d "%~dp0"
title fund_advice - Windows launcher
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_windows.ps1" %*
set "LAUNCH_EXIT=%ERRORLEVEL%"
if not "%LAUNCH_EXIT%"=="0" (
  echo.
  echo Startup failed. Read the error above. Logs: runtime\logs
  pause
)
exit /b %LAUNCH_EXIT%
