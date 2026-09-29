@echo off
REM ============================================================================
REM  Start the SANA GTM platform worker on this PC (scraper runs, research, exports…).
REM
REM    run-sanagtm-worker.bat
REM
REM  - settings: cloud\worker\.env.sana-cloud (git-ignored): the staging Supabase
REM    PostgreSQL + the staging Upstash Redis, queue sanagtm:staging:platform
REM  - maintenance every 30 s reaps expired leases, re-rings orphaned tasks and marks
REM    scraper runs whose task died, so work resumes after a crash or reboot
REM
REM  Never touches production CareerCrawler, state\crawler.db, Google Sheets,
REM  Seamless or ZoomInfo.
REM ============================================================================
setlocal EnableExtensions

pushd "%~dp0"
set "PYTHON=%CD%\cloud\.venv\Scripts\python.exe"
set "ENV_FILE=%CD%\cloud\worker\.env.sana-cloud"

if not exist "%ENV_FILE%" (
    echo  [X] Missing %ENV_FILE%  ^(see cloud\README.md, "SANA GTM staging"^)
    goto :fail
)

echo  Starting the SANA GTM platform worker ... (Ctrl+C to stop)
"%PYTHON%" -m cloud.intel.tasks.worker --env-file "%ENV_FILE%"
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%

:fail
popd
exit /b 1
