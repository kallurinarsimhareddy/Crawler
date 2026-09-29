@echo off
REM ============================================================================
REM  start-sana-gtm.bat
REM
REM  Start SANA GTM on this PC: the supervisor starts the API, the platform worker and
REM  the Cloudflare quick tunnel, waits for them, and restarts whatever stops.
REM
REM  Logs: logs\sana-gtm\   Script: deploy\windows\sana-gtm\sana-gtm.ps1
REM  Add /nopause to skip the final pause (e.g. when run from a script).
REM ============================================================================
setlocal
set "PS1=%~dp0deploy\windows\sana-gtm\sana-gtm.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Action start
set "CODE=%ERRORLEVEL%"
if /I not "%~1"=="/nopause" pause
exit /b %CODE%
