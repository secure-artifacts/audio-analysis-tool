@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_windows.ps1"
echo.
echo Window kept open so you can read any message above.
pause
