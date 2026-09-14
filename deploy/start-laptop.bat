@echo off
REM Starts the orchestrator and the dev tunnel on the control-plane machine.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-laptop.ps1" %*
echo.
pause
