<#
.SYNOPSIS
  Install SANA GTM on Windows: PostgreSQL 17, the API + dashboard, the background worker
  (PostgreSQL task queue, no Redis) and an optional Cloudflare Tunnel.

.DESCRIPTION
  Service mode (run as Administrator, recommended): Windows services that start at boot, before
  anyone signs in. User mode (no administrator): the same programs under your account, started
  when you sign in. Application files go to -InstallDir; ALL data (database, uploads, logs,
  backups, configuration with secrets) goes to -DataDir, so upgrades and uninstalls never touch it.

.EXAMPLE
  # double-click INSTALL.cmd, or from an Administrator PowerShell in the unzipped folder:
  powershell -ExecutionPolicy Bypass -File .\Install-SanaGtm.ps1
.EXAMPLE
  .\Install-SanaGtm.ps1 -PreflightOnly
.EXAMPLE
  .\Install-SanaGtm.ps1 -Mode Service -DataDir D:\SanaGtmData -AnswersFile C:\secure\answers.env
#>
[CmdletBinding()]
param(
    [ValidateSet('', 'Service', 'User')][string]$Mode = '',
    [string]$InstallDir = '',
    [string]$DataDir = '',
    [string]$ComponentsDir = '',
    [string]$AnswersFile = '',
    [int]$WebPort = 8100,
    [int]$PgPort = 55432,
    [int]$WorkerConcurrency = 2,
    [switch]$NoTunnel,
    [switch]$NoAutoStart,
    [switch]$ReuseData,
    [switch]$PreflightOnly,
    [switch]$SkipNetworkCheck
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$PkgRoot = $PSScriptRoot
Get-ChildItem -LiteralPath $PkgRoot -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
Import-Module (Join-Path $PkgRoot 'bin\lib\SanaGtm.psm1') -Force -DisableNameChecking
$Components = Get-Content -LiteralPath (Join-Path $PkgRoot 'components.json') -Raw | ConvertFrom-Json
$IsAdmin = Test-IsAdmin

# ---------------------------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------------------------
if (-not $Mode) { if ($IsAdmin) { $Mode = 'Service' } else { $Mode = 'User' } }
if (-not $InstallDir) {
    if ($Mode -eq 'Service') { $InstallDir = Join-Path $env:ProgramFiles 'SANA GTM' }
    else { $InstallDir = Join-Path $env:LOCALAPPDATA 'SANA GTM' }
}
if (-not $DataDir) {
    $d = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='D:' AND DriveType=3" -ErrorAction SilentlyContinue
    if ($d) { $DataDir = 'D:\SanaGtmData' } else { $DataDir = 'C:\SanaGtmData' }
}
$L = Get-SanaLayout $InstallDir $DataDir

# ---------------------------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------------------------
Write-Step "Preflight checks ($Mode mode)"
$blockers = New-Object Collections.Generic.List[string]
function Check([bool]$Ok, [string]$Text, [switch]$Blocker) {
    if ($Ok) { Write-Ok $Text } elseif ($Blocker) { Write-Bad $Text; $blockers.Add($Text) } else { Write-Warn2 $Text }
}
$os = Get-CimInstance Win32_OperatingSystem
Check ([Environment]::Is64BitOperatingSystem) "64-bit Windows: $($os.Caption) build $($os.BuildNumber)" -Blocker
Check ([int]$os.BuildNumber -ge 17763) 'Windows 10 1809 / Server 2019 or newer' -Blocker
Check ($PSVersionTable.PSVersion.Major -ge 5) "PowerShell $($PSVersionTable.PSVersion)" -Blocker
if ($Mode -eq 'Service') {
    Check $IsAdmin 'Administrator rights (needed to register Windows services)' -Blocker
} else {
    Check $true 'User mode: no administrator needed; SANA GTM starts when you sign in (not at boot)'
}
Write-Ok 'native install: no virtualization, WSL2 or Docker needed'
$ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
Check ($ramGb -ge 4) "memory $ramGb GB (8 GB recommended)" -Blocker
if ($ramGb -lt 8) { Write-Warn2 'less than 8 GB: the database will use smaller caches' }
function Get-FreeGb([string]$Path) {
    $root = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Path)).TrimEnd('\')
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$root'" -ErrorAction SilentlyContinue
    if ($disk) { return [math]::Round($disk.FreeSpace / 1GB) } else { return -1 }
}
$freeInstall = Get-FreeGb $L.InstallDir; $freeData = Get-FreeGb $L.DataDir
Check ($freeInstall -ge 3) "install drive for $($L.InstallDir): $freeInstall GB free (3 GB needed)" -Blocker
Check ($freeData -ge 20) "data drive for $($L.DataDir): $freeData GB free (20 GB minimum, 80 GB recommended)" -Blocker
foreach ($port in @($WebPort, $PgPort)) {
    $busy = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    Check (-not $busy) "port $port is free on this machine" -Blocker
}
$vc = Test-VcRuntime
if ($vc) { Write-Ok 'Microsoft Visual C++ 2015-2022 runtime present (PostgreSQL needs it)' }
elseif ($IsAdmin) { Write-Warn2 'Visual C++ runtime missing: it will be installed (Microsoft-signed redistributable)' }
else { Check $false 'Microsoft Visual C++ 2015-2022 x64 runtime missing: an administrator must install it (see README)' -Blocker }
$hasData = (Test-Path -LiteralPath $L.PgData) -and @(Get-ChildItem -LiteralPath $L.PgData -Force -ErrorAction SilentlyContinue).Count -gt 0
Check ((-not $hasData) -or $ReuseData) "no existing database in $($L.PgData) (use -ReuseData to keep one)" -Blocker
$offline = $ComponentsDir -and @(@('python', 'postgresql', 'winsw', 'cloudflared') | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $ComponentsDir $Components.$_.file)) }).Count -eq 0
if (-not $SkipNetworkCheck) {
    $targets = @(@{ h = 'supabase.co'; p = 443; need = $false; what = 'Supabase Auth (sign-in)' })
    if (-not $offline) {
        $targets += @{ h = 'api.nuget.org'; p = 443; need = $true; what = 'Python runtime download' }
        $targets += @{ h = 'get.enterprisedb.com'; p = 443; need = $true; what = 'PostgreSQL download' }
        $targets += @{ h = 'github.com'; p = 443; need = $true; what = 'service wrapper + cloudflared download' }
        $targets += @{ h = 'pypi.org'; p = 443; need = $true; what = 'Python packages' }
    }
    if (-not $NoTunnel) { $targets += @{ h = 'region1.v2.argotunnel.com'; p = 7844; need = $false; what = 'Cloudflare Tunnel edge' } }
    foreach ($t in $targets) {
        $ok = $false
        try { $c = New-Object Net.Sockets.TcpClient; $ok = $c.ConnectAsync($t.h, $t.p).Wait(5000) -and $c.Connected; $c.Close() } catch { }
        if ($t.need) { Check $ok "$($t.what): $($t.h):$($t.p) reachable" -Blocker } else { Check $ok "$($t.what): $($t.h):$($t.p) reachable" }
    }
}
if ($blockers.Count -gt 0) {
    Write-Host ''
    Write-Host "$($blockers.Count) blocker(s); nothing was changed." -ForegroundColor Red
    exit 1
}
if ($PreflightOnly) { Write-Host ''; Write-Host 'Preflight passed; nothing was changed (-PreflightOnly).' -ForegroundColor Green; exit 0 }

# ---------------------------------------------------------------------------------------------
# answers (only what is required; secrets are read hidden or from files, never echoed)
# ---------------------------------------------------------------------------------------------
Write-Step 'Configuration'
$answers = @{}
if ($AnswersFile) { $answers = Read-KeyValueFile $AnswersFile }
function Ask([string]$Key, [string]$Prompt, [switch]$Secret, [switch]$Optional) {
    if ($answers.Contains($Key) -and $answers[$Key]) { return $answers[$Key] }
    $fileKey = "${Key}_FILE"
    if ($answers.Contains($fileKey) -and $answers[$fileKey]) { return ([IO.File]::ReadAllText($answers[$fileKey])).Trim() }
    if ($AnswersFile -and $Optional) { return '' }
    if ($AnswersFile) { Stop-WithError "$Key (or ${Key}_FILE) is missing from the answers file" }
    if ($Secret) { $v = ConvertFrom-SecureToPlain (Read-Host -Prompt $Prompt -AsSecureString) } else { $v = Read-Host -Prompt $Prompt }
    return $v.Trim()
}
$hostname = Ask 'HOSTNAME' 'Public hostname for SANA GTM (e.g. sanagtm.example.com)'
if ($hostname -notmatch '^[a-z0-9.-]+\.[a-z]{2,}$') { Stop-WithError 'the hostname must look like sanagtm.example.com' }
if ($hostname -match '(^|[^a-z])(dev|test|stag)') { Stop-WithError 'the hostname looks like a dev/test/staging name; production refuses those' }
$supabaseUrl = (Ask 'SUPABASE_URL' 'Supabase Auth URL (https://<ref>.supabase.co)').TrimEnd('/')
if ($supabaseUrl -notmatch '^https://[a-z0-9.-]+$') { Stop-WithError 'the auth URL must be https://<host>' }
$anonKey = Ask 'SUPABASE_ANON_KEY' 'Supabase anon/publishable key (public)'
if ($anonKey -match 'service_role' -or $anonKey.StartsWith('sb_secret_')) { Stop-WithError 'that is a SECRET key; use the public anon/publishable key' }
$secretsKey = Ask 'PLATFORM_SECRETS_KEY' 'Existing CAREERCLOUD_PLATFORM_SECRETS_KEY (hidden; Enter = generate a new one)' -Secret -Optional
$newKey = $false
if (-not $secretsKey) { $secretsKey = New-FernetKey; $newKey = $true }
try { $raw = [Convert]::FromBase64String($secretsKey.Replace('-', '+').Replace('_', '/')) } catch { $raw = @() }
if ($raw.Length -ne 32) { Stop-WithError 'CAREERCLOUD_PLATFORM_SECRETS_KEY must be a 44-character Fernet key' }
$tunnelToken = ''
if (-not $NoTunnel) {
    $tunnelToken = Ask 'TUNNEL_TOKEN' 'Cloudflare Tunnel token (hidden)' -Secret
    if ($tunnelToken.Length -lt 50) { Stop-WithError 'that does not look like a Cloudflare Tunnel token' }
}
$legacy = ''
if ($answers.Contains('LEGACY_DATABASE_HOSTS')) { $legacy = $answers['LEGACY_DATABASE_HOSTS'] }
# Other browser origins that call this API, e.g. the Cloudflare Pages dashboard
# (https://sanagtm.pages.dev). The install's own hostname is always allowed.
$corsOrigins = @("https://$hostname")
if ($answers.Contains('EXTRA_CORS_ORIGINS') -and $answers['EXTRA_CORS_ORIGINS']) {
    foreach ($origin in ($answers['EXTRA_CORS_ORIGINS'] -split ',')) {
        $origin = $origin.Trim().TrimEnd('/')
        if (-not $origin) { continue }
        if ($origin -notmatch '^https://[a-z0-9.-]+\.[a-z]{2,}$') { Stop-WithError "EXTRA_CORS_ORIGINS: '$origin' must be an https origin like https://sanagtm.pages.dev" }
        if ($corsOrigins -notcontains $origin) { $corsOrigins += $origin }
    }
}
Write-Ok "hostname https://$hostname, auth $supabaseUrl, tunnel $(if ($NoTunnel) { 'off' } else { 'on' })"

# ---------------------------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------------------------
Write-Step "Folders: program $($L.InstallDir), data $($L.DataDir)"
foreach ($dir in @($L.InstallDir, $L.DataDir, $L.Config, $L.Files, $L.Tmp, $L.Results, $L.Run, $L.Backups, $L.Logs)) {
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}
Protect-SanaPath -Path $L.Config -Recurse
Write-Ok 'configuration folder restricted to SYSTEM, Administrators and you'

Write-Step 'Runtimes (verified SHA-256)'
$dl = Join-Path $L.InstallDir 'downloads'
$pyZip = Get-VerifiedComponent $Components.python $ComponentsDir $dl
$pgZip = Get-VerifiedComponent $Components.postgresql $ComponentsDir $dl
if (-not (Test-Path -LiteralPath $L.Python)) { Expand-ZipEntries $pyZip 'tools/' (Join-Path $L.Runtime 'python') }
if (-not (Test-Path -LiteralPath (Join-Path $L.PgBin 'postgres.exe'))) {
    Expand-ZipEntries $pgZip 'pgsql/' (Join-Path $L.Runtime 'pgsql') @('bin', 'lib', 'share')
}
Write-Ok "Python $($Components.python.version), PostgreSQL $($Components.postgresql.version)"
if ($Mode -eq 'Service') {
    $ws = Get-VerifiedComponent $Components.winsw $ComponentsDir $dl
    New-Item -ItemType Directory -Path (Join-Path $L.Runtime 'winsw') -Force | Out-Null
    Copy-Item -LiteralPath $ws -Destination (Join-Path $L.Runtime 'winsw\WinSW-x64.exe') -Force
    Write-Ok "WinSW $($Components.winsw.version)"
}
if (-not $NoTunnel) {
    $cf = Get-VerifiedComponent $Components.cloudflared $ComponentsDir $dl
    New-Item -ItemType Directory -Path (Split-Path $L.Cloudflared) -Force | Out-Null
    Copy-Item -LiteralPath $cf -Destination $L.Cloudflared -Force
    Write-Ok "cloudflared $($Components.cloudflared.version)"
}
if (-not (Test-VcRuntime)) { Install-VcRuntime $dl; Write-Ok 'Visual C++ runtime installed' }

Write-Step 'Application'
foreach ($part in @('app', 'web', 'bin')) {
    $target = Join-Path $L.InstallDir $part
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Copy-Item -LiteralPath (Join-Path $PkgRoot $part) -Destination $target -Recurse -Force
}
foreach ($f in @('components.json', 'requirements-lock.txt', 'README-WINDOWS.md', 'PACKAGE-INFO.json')) {
    if (Test-Path -LiteralPath (Join-Path $PkgRoot $f)) { Copy-Item -LiteralPath (Join-Path $PkgRoot $f) -Destination $L.InstallDir -Force }
}
Set-Content -LiteralPath (Join-Path $L.Bin 'sanagtm.location') -Value $L.DataDir -Encoding ascii
$pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-warn-script-location', '-q',
             '-r', (Join-Path $L.InstallDir 'requirements-lock.txt'))
if ($ComponentsDir -and (Test-Path -LiteralPath (Join-Path $ComponentsDir 'wheels'))) {
    $pipArgs += @('--no-index', '--find-links', (Join-Path $ComponentsDir 'wheels'))
}
$r = Invoke-Captured -FilePath $L.Python -Arguments $pipArgs -TimeoutSeconds 1800
if ($r.ExitCode -ne 0) { Stop-WithError "installing Python packages failed: $($r.StdErr)" }
Write-Ok "Python packages installed from the pinned lock file"

Write-Step 'Secrets and settings (config folder only)'
$super = New-RandomHex 24; $app = New-RandomHex 24
if ($ReuseData -and (Test-Path -LiteralPath $L.PgPasswordFile)) {
    $super = ([IO.File]::ReadAllText($L.PgPasswordFile)).Trim()
    $old = Read-KeyValueFile $L.EnvFile
    if ($old.Contains('CAREERCLOUD_DATABASE_URL') -and $old['CAREERCLOUD_DATABASE_URL'] -match 'sanagtm:([0-9a-f]+)@') { $app = $Matches[1] }
}
Write-SecretFile -Path $L.PgPasswordFile -Content $super
$lines = @(
    '# SANA GTM settings. SECRETS - readable only by SYSTEM, Administrators, the installing user and the services.',
    'CAREERCLOUD_ENV=production', 'CAREERCLOUD_SELF_HOSTED=1', 'CAREERCLOUD_STORAGE=postgres', 'CAREERCLOUD_QUEUE=postgres',
    "CAREERCLOUD_DATABASE_URL=postgresql://sanagtm:$app@127.0.0.1:$PgPort/sanagtm",
    'CAREERCLOUD_DB_USER_ROLE=authenticated', 'CAREERCLOUD_DB_POOL_MAX=8', 'CAREERCLOUD_AUTH_MODE=supabase',
    "CAREERCLOUD_SUPABASE_URL=$supabaseUrl", "CAREERCLOUD_API_ORIGIN=https://$hostname", "CAREERCLOUD_CORS_ORIGINS=$($corsOrigins -join ',')",
    'CAREERCLOUD_TRUST_PROXY=cloudflare', 'CAREERCLOUD_LOG_FORMAT=json', 'CAREERCLOUD_LOG_LEVEL=INFO',
    'CAREERCLOUD_STORAGE_BACKEND=local', 'CAREERCLOUD_RUNNER=none', "CAREERCLOUD_PLATFORM_SECRETS_KEY=$secretsKey",
    "CAREERCLOUD_PLATFORM_FILES_DIR=$($L.Files)", "CAREERCLOUD_RESULTS_DIR=$($L.Results)",
    "CAREERCLOUD_WORKER_STATE_FILE=$(Join-Path $L.Run 'worker-state.json')",
    "CAREERCLOUD_RESOURCE_REGISTRY=$(Join-Path $L.Run 'resources.json')",
    "SANAGTM_PUBLIC_SUPABASE_URL=$supabaseUrl", "SANAGTM_PUBLIC_SUPABASE_ANON_KEY=$anonKey",
    "SANAGTM_WEB_DIST=$($L.Web)", "SANAGTM_API_PORT=$WebPort", "SANAGTM_LEGACY_DATABASE_HOSTS=$legacy",
    "TMP=$($L.Tmp)", "TEMP=$($L.Tmp)", "TMPDIR=$($L.Tmp)"
)
foreach ($k in $answers.Keys) { if ($k.StartsWith('ENV_')) { $lines += "$($k.Substring(4))=$($answers[$k])" } }
Write-SecretFile -Path $L.EnvFile -Content (($lines -join "`r`n") + "`r`n")
if (-not $NoTunnel) { Write-SecretFile -Path $L.TunnelTokenFile -Content $tunnelToken }
$secretsKey = $null; $tunnelToken = $null
$info = [ordered]@{
    Mode = $Mode; InstallDir = $L.InstallDir; DataDir = $L.DataDir; WebPort = $WebPort; PgPort = $PgPort
    WorkerConcurrency = $WorkerConcurrency; Hostname = $hostname; Tunnel = (-not $NoTunnel)
    InstalledAt = (Get-Date).ToUniversalTime().ToString('s') + 'Z'; Package = $null
}
if (Test-Path -LiteralPath (Join-Path $PkgRoot 'PACKAGE-INFO.json')) {
    $info.Package = (Get-Content -LiteralPath (Join-Path $PkgRoot 'PACKAGE-INFO.json') -Raw | ConvertFrom-Json)
}
[IO.File]::WriteAllText($L.InstallInfo, ($info | ConvertTo-Json -Depth 4))
if ($newKey) { Write-Warn2 'a NEW platform secrets key was generated: provider credentials from another install cannot be read with it. Back up config\sanagtm.env.' }
Write-Ok 'settings written (no secret was displayed)'

Write-Step 'PostgreSQL 17 (127.0.0.1 only)'
if (-not $hasData) { Initialize-PgCluster $L $PgPort $super $app }
Start-PgDirect $L $PgPort
if (-not $hasData) { Initialize-PgDatabase $L $PgPort $super $app }
Write-Ok "PostgreSQL listening on 127.0.0.1:$PgPort only"

Write-Step 'Database schema (migrations + environment stamp)'
$r = Invoke-SanaPython $L @('-m', 'cloud.ops.selfhost', 'prepare') @{ SANAGTM_DB_WAIT_SECONDS = '60' }
if ($r.ExitCode -ne 0) { Stop-WithError "database preparation failed: $($r.StdErr.Trim())" }
Write-Ok ($r.StdOut.Trim().Split("`n")[-1])
if ($ReuseData) {
    $r = Invoke-SanaPython $L @('-m', 'cloud.ops.selfhost', 'verify-secrets')
    if ($r.ExitCode -ne 0) { Write-Warn2 "stored provider credentials are NOT readable with this key: $($r.StdOut.Trim())" }
    else { Write-Ok "stored provider credentials readable: $($r.StdOut.Trim())" }
}

if ($Mode -eq 'Service') {
    Write-Step 'Windows services (start at boot, no sign-in needed)'
    Stop-PgDirect $L | Out-Null
    Set-ServiceModePermissions $L
    Install-SanaServices $L $info
    $names = 'SanaGtmPostgres, SanaGtmWeb, SanaGtmWorker'
    if (-not $NoTunnel) { $names += ', SanaGtmTunnel' }
    Write-Ok "$names registered (delayed automatic start, restart on failure)"
} elseif (-not $NoAutoStart) {
    Write-Step 'Start at sign-in (scheduled task, no administrator)'
    Register-UserLogonTask $L
    Write-Ok "task 'SANA GTM (self-hosted)' starts SANA GTM when $env:USERNAME signs in"
}

Write-Step 'Starting'
& (Join-Path $L.Bin 'sanagtm.ps1') start
& (Join-Path $L.Bin 'sanagtm.ps1') health
Write-Host ''
Write-Host 'SANA GTM is installed.' -ForegroundColor Green
Write-Host "  URL:       https://$hostname   (locally: http://127.0.0.1:$WebPort)"
Write-Host "  Manage:    `"$($L.Bin)\sanagtm.cmd`" status | health | logs | backup | restore | update | uninstall"
Write-Host "  Data:      $($L.DataDir)   (database, uploads, logs, backups, configuration)"
