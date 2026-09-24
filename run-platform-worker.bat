@echo off
REM ============================================================================
REM  Start the CareerCrawler platform worker (cloud/intel) on Windows.
REM
REM    run-platform-worker.bat                     use cloud\worker\.env
REM    run-platform-worker.bat path\to\other.env   use another environment file
REM
REM  It runs the platform's background tasks: careers crawls into the job
REM  master, discovery, scraping, enrichment, email validation, research,
REM  imports, exports, monitoring, signals and workflows. It runs BESIDE the
REM  CareerCloud crawl worker (run-worker.bat) on its own queue prefix
REM  (<CAREERCLOUD_QUEUE_PREFIX>:platform).
REM
REM  Same guarantees as run-worker.bat: CAREERCLOUD_* settings only, never the
REM  production state\crawler.db, checkpoint, output\, secrets\ or Google Sheet,
REM  never the weekly run's worker count. It never sends email and never spends
REM  provider credits without an explicit, approved task.
REM
REM  On Linux/macOS or any VPS: python -m cloud.intel.tasks.worker --env-file <file>
REM ============================================================================
setlocal EnableExtensions

pushd "%~dp0"
set "REPO_ROOT=%CD%"
set "PYTHON=%REPO_ROOT%\cloud\.venv\Scripts\python.exe"
set "ENV_FILE=%~1"
if "%ENV_FILE%"=="" set "ENV_FILE=%REPO_ROOT%\cloud\worker\.env"

if not exist "%PYTHON%" (
    echo  [X] Missing virtualenv %PYTHON%
    echo      py -3.12 -m venv cloud\.venv
    echo      cloud\.venv\Scripts\python -m pip install -r cloud\requirements-dev.txt
    goto :fail
)
if not exist "%ENV_FILE%" (
    echo  [X] No environment file at %ENV_FILE%  ^(copy cloud\worker\.env.example^)
    goto :fail
)
findstr /R /I /C:"^ *CAREERCLOUD_ENV *= *production" "%ENV_FILE%" >nul 2>&1
if not errorlevel 1 (
    echo  [X] %ENV_FILE% sets CAREERCLOUD_ENV=production; deploy production to a managed host instead.
    goto :fail
)
findstr /R /I /C:"crawler\.db" /C:"sqlite" "%ENV_FILE%" >nul 2>&1
if not errorlevel 1 (
    echo  [X] %ENV_FILE% mentions SQLite or crawler.db. The platform uses PostgreSQL only.
    goto :fail
)

echo  Starting the platform worker with %ENV_FILE% ... (Ctrl+C to stop)
"%PYTHON%" -m cloud.intel.tasks.worker --env-file "%ENV_FILE%"
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%

:fail
popd
exit /b 1
