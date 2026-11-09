@echo off
rem Double-click to start V.A.R.A. Passes any options through, e.g. start-vara.cmd -Stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-vara.ps1" %*
if errorlevel 1 pause
