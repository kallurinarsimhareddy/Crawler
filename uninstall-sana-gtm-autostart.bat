@echo off
REM ============================================================================
REM  uninstall-sana-gtm-autostart.bat
REM
REM  Remove the "SANA GTM Auto Start" scheduled task (running services are left alone).
REM
REM  Logs: logs\sana-gtm\   Script: deploy\windows\sana-gtm\sana-gtm.ps1
REM  Add /nopause to skip the final pause (e.g. when run from a script).
REM ============================================================================
setlocal
set "PS1=%~dp0deploy\windows\sana-gtm\sana-gtm.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Action uninstall
set "CODE=%ERRORLEVEL%"
if /I not "%~1"=="/nopause" pause
exit /b %CODE%
