@echo off
rem SANA GTM management: sanagtm start ^| stop ^| restart ^| status ^| health ^| logs ^| backup ^| restore ^| update ^| uninstall
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0sanagtm.ps1" %*
exit /b %ERRORLEVEL%
