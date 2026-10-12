@echo off
rem Double-click to install SANA GTM. Right-click > Run as administrator for the recommended service mode.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-SanaGtm.ps1" %*
echo.
pause
