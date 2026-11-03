@echo off
rem Removes Retrace, keeping your rounds. This whole folder is deleted a few seconds after it finishes.
title Remove Retrace
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Uninstall -InstallDir "%~dp0." %* & pause & exit /b
