@echo off
REM ============================================================================
REM  Stop the local CareerCloud worker gracefully.
REM
REM  Writes the stop file that run-worker.bat told the worker to watch. The
REM  worker finishes the company it is crawling, puts any unfinished job back on
REM  the queue with its attempt refunded, and exits. Nothing is lost and no
REM  retry is burned.
REM
REM  This is the polite stop. Closing the worker's window kills it instead; the
REM  job's lease then expires and another worker requeues it, which also works
REM  but takes a minute or so.
REM ============================================================================
setlocal EnableExtensions

pushd "%~dp0"
set "REPO_ROOT=%CD%"
if "%CAREERCLOUD_STOP_FILE%"=="" set "CAREERCLOUD_STOP_FILE=%REPO_ROOT%\cloud\.localdev\worker.stop"

for %%D in ("%CAREERCLOUD_STOP_FILE%") do if not exist "%%~dpD" mkdir "%%~dpD" >nul 2>&1
echo stop requested %DATE% %TIME%> "%CAREERCLOUD_STOP_FILE%"

if not exist "%CAREERCLOUD_STOP_FILE%" (
    echo  [X] Could not write the stop file:
    echo      %CAREERCLOUD_STOP_FILE%
    echo      Close the worker's window instead.
    popd
    endlocal & exit /b 1
)

echo.
echo   Stop requested.
echo   file: %CAREERCLOUD_STOP_FILE%
echo.
echo   The worker checks once a second. It will finish the company it is on
echo   and then exit, so this can take a moment on a slow career site.
echo   Watch the worker's window: it prints "worker ... stopped" when done.
echo.
echo   If no worker is running, this file is simply ignored; the next
echo   run-worker.bat clears it at startup.
echo.
popd
endlocal & exit /b 0
