@echo off
REM ============================================================================
REM  status-sana-gtm.bat
REM
REM  Show PostgreSQL / API / Worker / Tunnel: ONLINE/OFFLINE and Overall: READY/NOT READY.
REM
REM  Logs: logs\sana-gtm\   Script: deploy\windows\sana-gtm\sana-gtm.ps1
REM  Add /nopause to skip the final pause (e.g. when run from a script).
REM ============================================================================
setlocal
set "PS1=%~dp0deploy\windows\sana-gtm\sana-gtm.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Action status
set "CODE=%ERRORLEVEL%"
if /I not "%~1"=="/nopause" pause
exit /b %CODE%
