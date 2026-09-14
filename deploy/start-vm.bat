@echo off
REM Starts the Foundry bridge and the agent node on an Azure VM.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-vm.ps1" %*
echo.
pause
