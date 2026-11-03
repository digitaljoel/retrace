@echo off
rem Double-click to install or update Retrace. Everything it does is in install.ps1, next to this file.
title Retrace setup
if not exist "%~dp0install.ps1" (
  echo.
  echo Please unzip the whole Retrace folder first, then double-click Install.bat inside it.
  echo.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RESULT=%ERRORLEVEL%
echo.
pause
exit /b %RESULT%
