@echo off
REM ============================================================================
REM  Start the SANA GTM staging API that https://sanagtm.pages.dev talks to.
REM
REM    run-sanagtm-staging.bat
REM
REM  - Supabase auth (staging project) via cloud\api\.env.sana-staging (git-ignored)
REM  - its own database, sanagtm_staging, on the embedded PostgreSQL (separate from
REM    local development data), port 8100 on 127.0.0.1 only
REM  - then open a Cloudflare quick tunnel in another window:
REM      cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8100
REM    A quick tunnel gets a NEW https://*.trycloudflare.com URL every time. When it
REM    changes, put it in cloud\web\.env.staging.local (VITE_API_URL), then
REM      cd cloud\web ^&^& npm run build:staging
REM      npx wrangler pages deploy dist --project-name sanagtm --branch main
REM    (see cloud\README.md, "SANA GTM staging").
REM
REM  Never touches production CareerCrawler, state\crawler.db, Google Sheets,
REM  Seamless or ZoomInfo.
REM ============================================================================
setlocal EnableExtensions

pushd "%~dp0"
set "PYTHON=%CD%\cloud\.venv\Scripts\python.exe"
set "ENV_FILE=%CD%\cloud\api\.env.sana-staging"

if not exist "%ENV_FILE%" (
    echo  [X] Missing %ENV_FILE%  ^(see cloud\README.md, "SANA GTM staging"^)
    goto :fail
)

for /f "delims=" %%U in ('"%PYTHON%" -m cloud.devtools.localpg start 2^>nul') do set "BASE_URL=%%U"
if "%BASE_URL%"=="" (
    echo  [X] The embedded PostgreSQL did not start.
    goto :fail
)
REM localpg prints ...@127.0.0.1:<port>/postgres; swap only that trailing database name.
if not "%BASE_URL:~-9%"=="/postgres" (
    echo  [X] Unexpected embedded PostgreSQL URL.
    goto :fail
)
set "CAREERCLOUD_DATABASE_URL=%BASE_URL:~0,-9%/sanagtm_staging"

echo  Starting the SANA GTM staging API on http://127.0.0.1:8100 ... (Ctrl+C to stop)
"%PYTHON%" -m uvicorn cloud.api.main:app --host 127.0.0.1 --port 8100 --env-file "%ENV_FILE%" --proxy-headers
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%

:fail
popd
exit /b 1
