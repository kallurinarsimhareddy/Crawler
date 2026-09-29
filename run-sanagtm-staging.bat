@echo off
REM ============================================================================
REM  Start the SANA GTM API that https://sanagtm.pages.dev talks to (this PC).
REM
REM    run-sanagtm-staging.bat
REM
REM  - settings: cloud\api\.env.sana-cloud (git-ignored): Supabase auth + the staging
REM    Supabase PostgreSQL + the staging Upstash Redis (prefix sanagtm:staging)
REM  - listens on 127.0.0.1:8100 only; the platform worker is run-sanagtm-worker.bat
REM  - public access: a Cloudflare quick tunnel in another window:
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
set "ENV_FILE=%CD%\cloud\api\.env.sana-cloud"

if not exist "%ENV_FILE%" (
    echo  [X] Missing %ENV_FILE%  ^(see cloud\README.md, "SANA GTM staging"^)
    goto :fail
)

echo  Starting the SANA GTM API on http://127.0.0.1:8100 ... (Ctrl+C to stop)
"%PYTHON%" -m uvicorn cloud.api.main:app --host 127.0.0.1 --port 8100 --env-file "%ENV_FILE%" --proxy-headers
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%

:fail
popd
exit /b 1
