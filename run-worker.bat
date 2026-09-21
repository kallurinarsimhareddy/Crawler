@echo off
REM ============================================================================
REM  Start the local CareerCloud worker on Windows.
REM
REM    run-worker.bat                     use cloud\worker\.env
REM    run-worker.bat path\to\other.env   use another environment file
REM
REM  The worker is the process that actually crawls. It takes job ids off the
REM  queue, runs the existing CareerCrawler engine in its own scratch space and
REM  uploads the results. Until it is running, the dashboard says
REM  "Crawler worker offline" and queued crawls simply wait.
REM
REM  This script only ever reads CAREERCLOUD_* settings. It does not touch the
REM  production CareerCrawler: not state\crawler.db, not its checkpoint, not
REM  output\, secrets\ or the Google Sheet, and it never changes the weekly
REM  run's worker count. Those guarantees are enforced in Python as well
REM  (cloud/tests/test_isolation.py), not just here.
REM
REM  Stop it with stop-worker.bat, or Ctrl+C in this window.
REM ============================================================================
setlocal EnableExtensions

pushd "%~dp0"
set "REPO_ROOT=%CD%"
set "PYTHON=%REPO_ROOT%\cloud\.venv\Scripts\python.exe"
set "ENV_FILE=%~1"
if "%ENV_FILE%"=="" set "ENV_FILE=%REPO_ROOT%\cloud\worker\.env"

echo.
echo  ===========================================================
echo   CareerCloud worker
echo  ===========================================================
echo   repo        : %REPO_ROOT%
echo   env file    : %ENV_FILE%
echo.

REM --- 1. the virtualenv must exist -----------------------------------------
if not exist "%PYTHON%" (
    echo  [X] CareerCloud's virtualenv is missing:
    echo      %PYTHON%
    echo.
    echo      Create it once, from this folder:
    echo        py -3.12 -m venv cloud\.venv
    echo        cloud\.venv\Scripts\python -m pip install -r cloud\requirements-dev.txt
    goto :fail
)

REM --- 2. the environment file must exist ------------------------------------
if not exist "%ENV_FILE%" (
    echo  [X] No worker environment file at:
    echo      %ENV_FILE%
    echo.
    echo      Create one from the template and fill it in:
    echo        copy cloud\worker\.env.example cloud\worker\.env
    echo.
    echo      It needs at least CAREERCLOUD_DATABASE_URL and CAREERCLOUD_REDIS_URL.
    goto :fail
)

REM --- 3. refuse to run as production ----------------------------------------
REM  This launcher is for a developer machine. A production worker belongs on a
REM  managed host with its own secret store, not in a console window someone can
REM  close. Staging is allowed: that is what this machine is for right now.
findstr /R /I /C:"^ *CAREERCLOUD_ENV *= *production" "%ENV_FILE%" >nul 2>&1
if not errorlevel 1 (
    echo  [X] %ENV_FILE% sets CAREERCLOUD_ENV=production.
    echo.
    echo      run-worker.bat will not start a production worker. Use
    echo      development or staging here, and deploy production to a real host
    echo      ^(see cloud\README.md -^> Running the worker on another host^).
    goto :fail
)

REM --- 4. refuse anything pointing at the production crawler's SQLite ---------
findstr /R /I /C:"crawler\.db" /C:"sqlite" "%ENV_FILE%" >nul 2>&1
if not errorlevel 1 (
    echo  [X] %ENV_FILE% mentions a SQLite database or crawler.db.
    echo.
    echo      CareerCloud uses PostgreSQL only. The production crawler's
    echo      state\crawler.db must never be opened by a cloud worker.
    goto :fail
)

REM --- 5. graceful-stop file -------------------------------------------------
REM  stop-worker.bat creates this file; the worker sees it, finishes the company
REM  it is on and exits. Same effect as SIGTERM, which cmd.exe cannot send.
if "%CAREERCLOUD_STOP_FILE%"=="" set "CAREERCLOUD_STOP_FILE=%REPO_ROOT%\cloud\.localdev\worker.stop"
for %%D in ("%CAREERCLOUD_STOP_FILE%") do if not exist "%%~dpD" mkdir "%%~dpD" >nul 2>&1
if exist "%CAREERCLOUD_STOP_FILE%" del /q "%CAREERCLOUD_STOP_FILE%" >nul 2>&1

echo   stop file   : %CAREERCLOUD_STOP_FILE%
echo.
echo   Starting. The worker connects to the database and queue named in the
echo   environment file, then waits for crawls. Leave this window open.
echo   Stop it with stop-worker.bat, or press Ctrl+C here.
echo.
echo  -----------------------------------------------------------
echo.

"%PYTHON%" -m cloud.worker --env-file "%ENV_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo  -----------------------------------------------------------
if "%EXIT_CODE%"=="0" (
    echo   Worker stopped cleanly. No crawl was lost: anything half-done
    echo   went back on the queue and will be picked up next start.
    goto :done
)
if "%EXIT_CODE%"=="2" (
    echo  [X] Configuration error ^(exit 2^).
    echo      A CAREERCLOUD_* value in %ENV_FILE% is missing or unusable.
    echo      The message above names it.
    goto :done
)
if "%EXIT_CODE%"=="3" (
    echo  [X] Environment isolation refused to start the worker ^(exit 3^).
    echo      Its database, queue prefix or bucket does not match the
    echo      environment it claims to be. Nothing was touched. This is the
    echo      guard that stops a staging worker writing to production.
    goto :done
)
echo  [X] Worker exited with code %EXIT_CODE%.
echo      Common causes: the database or Redis is unreachable, or the
echo      credentials in %ENV_FILE% are wrong. The traceback above says which.

:done
echo.
popd
endlocal & exit /b %EXIT_CODE%

:fail
echo.
popd
endlocal & exit /b 1
