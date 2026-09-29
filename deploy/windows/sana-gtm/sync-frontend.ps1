<#
  SANA GTM frontend <-> quick tunnel URL sync.

    sync-frontend.ps1 -Mode check    compare the live tunnel URL with the API URL the LIVE
                                     frontend (https://sanagtm.pages.dev) was built with
    sync-frontend.ps1 -Mode sync     if they differ: update VITE_API_URL in
                                     cloud\web\.env.staging.local, npm run build:staging,
                                     wrangler pages deploy, verify the live site

  A Cloudflare QUICK tunnel is not permanent: every cloudflared restart (every reboot)
  gets a new https://<words>.trycloudflare.com URL, and the frontend has the API URL
  baked in at build time (bundle + CSP connect-src). This script is the only thing
  that rebuilds/redeploys the frontend, and it refuses unless every guard passes:

    - the tunnel URL answers /api/v1/health publicly 3 times in a row
    - the live frontend really calls a different URL (read from its CSP header)
    - no other sync is running (lock file)
    - at most $MaxDeploysPerDay deploys in 24 h and $MaxAttemptsPerUrl attempts per URL
    - with -Auto (the supervisor): logs\sana-gtm\frontend-autosync.off must not exist

  History: logs\sana-gtm\frontend-sync.log (one line per decision), build/deploy output in
  frontend-build.log / frontend-deploy.log. Only public values are logged (URLs); the env
  file is edited in place for the one VITE_API_URL line and never printed.
#>
param(
    [ValidateSet("check", "sync")]
    [string]$Mode = "check",
    [switch]$Auto
)

$ErrorActionPreference = "Stop"

$Root         = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$LogDir       = Join-Path $Root "logs\sana-gtm"
$WebDir       = Join-Path $Root "cloud\web"
$WebEnv       = Join-Path $WebDir ".env.staging.local"
$UrlFile      = Join-Path $LogDir "tunnel-url.txt"
$SyncLog      = Join-Path $LogDir "frontend-sync.log"
$History      = Join-Path $LogDir "frontend-deploys.csv"
$LockFile     = Join-Path $LogDir "frontend-sync.lock"
$AutoOff      = Join-Path $LogDir "frontend-autosync.off"
$SiteUrl      = "https://sanagtm.pages.dev/"
$Project      = "sanagtm"
$NodeDir      = "C:\Program Files\nodejs"
$Python       = Join-Path $Root "cloud\.venv\Scripts\python.exe"
$Launcher     = Join-Path $PSScriptRoot "launch_hidden.py"
$MaxDeploysPerDay  = 6
$MaxAttemptsPerUrl = 2

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$Message, [string]$Level = "INFO") {
    $line = "{0} {1,-5} {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    # The supervisor calls this every 5 minutes: do not repeat the same refusal forever.
    $last = if (Test-Path $SyncLog) { Get-Content $SyncLog -Tail 1 -ErrorAction SilentlyContinue } else { "" }
    if ($Auto -and $last -and $last.Length -gt 20 -and $last.Substring(20) -eq $line.Substring(20)) { return }
    try { Add-Content -Path $SyncLog -Value $line -Encoding UTF8 } catch { }
    Write-Host $line
}

function Test-Http([string]$Url, [int]$TimeoutSec = 15) {
    try { return ((Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec).StatusCode -eq 200) }
    catch { return $false }
}

function Get-TunnelUrl {
    if (Test-Path $UrlFile) {
        $u = Get-Content $UrlFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($u -match '^https://[a-z0-9-]+\.trycloudflare\.com$') { return $u.Trim() }
    }
    return ""
}

# The API origin the LIVE frontend uses: build-staging.mjs puts exactly that origin in
# the CSP connect-src it deploys in _headers.
function Get-LiveFrontendApiUrl {
    try {
        $r = Invoke-WebRequest -Uri "$SiteUrl`?sync=$(Get-Random)" -UseBasicParsing -TimeoutSec 20 -Headers @{ "Cache-Control" = "no-cache" }
        $csp = [string]$r.Headers["Content-Security-Policy"]
        $m = [regex]::Match($csp, 'connect-src[^;]*?(https://[a-z0-9-]+\.trycloudflare\.com)')
        if ($m.Success) { return $m.Groups[1].Value }
    } catch { }
    return ""
}

function Get-EnvApiUrl {
    $line = Get-Content $WebEnv | Where-Object { $_ -match '^\s*VITE_API_URL\s*=' } | Select-Object -First 1
    if (-not $line) { return "" }
    return ($line -replace '^\s*VITE_API_URL\s*=\s*', '').Trim().TrimEnd('/')
}

function Set-EnvApiUrl([string]$Url) {
    $lines = @(Get-Content $WebEnv)
    $out = foreach ($l in $lines) { if ($l -match '^\s*VITE_API_URL\s*=') { "VITE_API_URL=$Url" } else { $l } }
    [System.IO.File]::WriteAllLines($WebEnv, [string[]]$out, (New-Object System.Text.UTF8Encoding($false)))
}

function Get-History {
    if (-not (Test-Path $History)) { return @() }
    @(Import-Csv $History)
}

function Add-History([string]$Url, [string]$Result) {
    [pscustomobject]@{ time = (Get-Date -Format o); url = $Url; result = $Result; auto = [bool]$Auto } |
        Export-Csv -Path $History -Append -NoTypeInformation -Encoding UTF8
}

function Invoke-Logged([string]$Exe, [string[]]$Arguments, [string]$Log) {
    # Output goes to a file only (build/deploy tools print URLs and file names, never the env file).
    # launch_hidden.py: CREATE_NO_WINDOW, so no console window appears (see sana-gtm.ps1).
    Remove-Item -Force $Log, "$Log.err" -ErrorAction SilentlyContinue
    & $Python $Launcher --cwd $WebDir --stdout $Log --stderr "$Log.err" --wait -- $Exe @Arguments
    return $LASTEXITCODE
}

# --- check ---------------------------------------------------------------------------------------

$tunnel = Get-TunnelUrl
$live = Get-LiveFrontendApiUrl
$tunnelOk = $tunnel -and (Test-Http "$tunnel/api/v1/health")
$state = if (-not $tunnel) { "tunnel-url-unknown" }
         elseif (-not $tunnelOk) { "tunnel-not-answering" }
         elseif (-not $live) { "frontend-url-unknown" }
         elseif ($live -eq $tunnel) { "in-sync" }
         else { "MISMATCH" }

if ($Mode -eq "check") {
    Write-Host "Tunnel URL (live):       $(if ($tunnel) { $tunnel } else { '?' })  $(if ($tunnelOk) { '(answering)' } else { '(NOT answering)' })"
    Write-Host "Frontend calls (live):   $(if ($live) { $live } else { '?' })"
    Write-Host "Frontend env file:       $(Get-EnvApiUrl)"
    Write-Host "State:                   $state"
    Write-Host "Auto-sync:               $(if (Test-Path $AutoOff) { 'OFF (frontend-autosync.off exists)' } else { 'ON' })"
    exit $(if ($state -eq "in-sync") { 0 } else { 1 })
}

# --- sync ----------------------------------------------------------------------------------------

if ($Auto -and (Test-Path $AutoOff)) { Write-Log "Auto-sync is off ($AutoOff); frontend not updated (state $state)" "WARN"; exit 2 }
if ($state -eq "in-sync") { if (-not $Auto) { Write-Log "Frontend already calls $tunnel; nothing to do" }; exit 0 }
if ($state -ne "MISMATCH") { Write-Log "Not syncing: $state (tunnel '$tunnel', frontend '$live')" "WARN"; exit 3 }

# Single run at a time; a lock older than 30 minutes is stale.
if ((Test-Path $LockFile) -and ((Get-Date) - (Get-Item $LockFile).LastWriteTime).TotalMinutes -lt 30) {
    Write-Log "Another frontend sync is running (lock $LockFile); skipping" "WARN"; exit 4
}
Set-Content -Path $LockFile -Value $PID -Encoding ASCII

try {
    $hist = Get-History
    $day = @($hist | Where-Object { $_.result -eq "deployed" -and ((Get-Date) - [datetime]$_.time).TotalHours -lt 24 })
    if ($day.Count -ge $MaxDeploysPerDay) {
        Write-Log "Refusing: $($day.Count) frontend deploys in the last 24 h (limit $MaxDeploysPerDay). Sync by hand if needed." "WARN"; exit 5
    }
    $tries = @($hist | Where-Object { $_.url -eq $tunnel })
    if ($tries.Count -ge $MaxAttemptsPerUrl) {
        Write-Log "Refusing: already $($tries.Count) attempts for $tunnel (limit $MaxAttemptsPerUrl). See frontend-build.log / frontend-deploy.log." "WARN"; exit 6
    }

    # The tunnel must be stable: 3 good public answers, 10 s apart.
    for ($i = 0; $i -lt 3; $i++) {
        if (-not (Test-Http "$tunnel/api/v1/health")) { Write-Log "Refusing: $tunnel stopped answering during the stability check" "WARN"; exit 7 }
        if ($i -lt 2) { Start-Sleep -Seconds 10 }
    }

    Write-Log "Frontend calls $live but the tunnel is $tunnel; rebuilding and redeploying $SiteUrl"
    $env:PATH = "$NodeDir;$env:PATH"
    $previous = Get-EnvApiUrl
    Set-EnvApiUrl $tunnel
    Write-Log "VITE_API_URL in cloud\web\.env.staging.local: $previous -> $tunnel"

    $code = Invoke-Logged (Join-Path $NodeDir "node.exe") @("scripts/build-staging.mjs") (Join-Path $LogDir "frontend-build.log")
    if ($code -ne 0) { Add-History $tunnel "build-failed"; Write-Log "Build failed (exit $code); see frontend-build.log(.err). Live site unchanged." "ERROR"; exit 8 }
    Write-Log "Build OK"

    $sha = (& git -C $Root rev-parse --short HEAD 2>$null)
    # npx through node.exe directly (npx.cmd would need cmd.exe).
    $deployArgs = @((Join-Path $NodeDir "node_modules\npm\bin\npx-cli.js"), "--yes", "wrangler@4", "pages", "deploy", "dist",
        "--project-name", $Project, "--branch", "main", "--commit-hash", $sha, "--commit-message", "tunnel URL sync", "--commit-dirty=true")
    $code = Invoke-Logged (Join-Path $NodeDir "node.exe") $deployArgs (Join-Path $LogDir "frontend-deploy.log")
    if ($code -ne 0) { Add-History $tunnel "deploy-failed"; Write-Log "wrangler pages deploy failed (exit $code); see frontend-deploy.log(.err). Live site unchanged." "ERROR"; exit 9 }
    $deployUrl = Select-String -Path (Join-Path $LogDir "frontend-deploy.log") -Pattern 'https://[a-z0-9]+\.sanagtm\.pages\.dev' |
        Select-Object -Last 1 | ForEach-Object { $_.Matches[0].Value }
    Write-Log "Deployed $deployUrl"

    # The production alias can take a moment to switch.
    for ($i = 0; $i -lt 12; $i++) {
        if ((Get-LiveFrontendApiUrl) -eq $tunnel) { break }
        Start-Sleep -Seconds 10
    }
    if ((Get-LiveFrontendApiUrl) -eq $tunnel) {
        Add-History $tunnel "deployed"
        Write-Log "VERIFIED: $SiteUrl now calls $tunnel"
        exit 0
    }
    Add-History $tunnel "deployed-unverified"
    Write-Log "Deployed, but $SiteUrl does not show the new URL yet" "WARN"
    exit 10
} finally {
    Remove-Item -Force $LockFile -ErrorAction SilentlyContinue
}
