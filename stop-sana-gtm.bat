@echo off
REM ============================================================================
REM  stop-sana-gtm.bat
REM
REM  Stop SANA GTM: the supervisor, the worker, the API and the tunnel.
REM  NOTE: the next start gives the quick tunnel a NEW public URL (frontend redeploy needed).
REM
REM  Logs: logs\sana-gtm\   Script: deploy\windows\sana-gtm\sana-gtm.ps1
REM  Add /nopause to skip the final pause (e.g. when run from a script).
REM ============================================================================
setlocal
set "PS1=%~dp0deploy\windows\sana-gtm\sana-gtm.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Action stop
set "CODE=%ERRORLEVEL%"
if /I not "%~1"=="/nopause" pause
exit /b %CODE%
