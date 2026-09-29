@echo off
REM ============================================================================
REM  install-sana-gtm-autostart.bat
REM
REM  Register the Windows scheduled task "SANA GTM Auto Start" (runs the supervisor
REM  hidden at every logon of this user).
REM
REM  Logs: logs\sana-gtm\   Script: deploy\windows\sana-gtm\sana-gtm.ps1
REM  Add /nopause to skip the final pause (e.g. when run from a script).
REM ============================================================================
setlocal
set "PS1=%~dp0deploy\windows\sana-gtm\sana-gtm.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Action install
set "CODE=%ERRORLEVEL%"
if /I not "%~1"=="/nopause" pause
exit /b %CODE%
