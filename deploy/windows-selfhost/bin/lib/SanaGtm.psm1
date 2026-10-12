# SANA GTM for Windows - shared functions for Install-SanaGtm.ps1 and sanagtm.ps1.
# Windows PowerShell 5.1 compatible. Secrets are never written to the console, never put on a
# command line, and only stored in <DataDir>\config with an access list limited to SYSTEM,
# Administrators, the installing user and (service mode) the service accounts.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:SidSystem = 'S-1-5-18'
$script:SidAdmins = 'S-1-5-32-544'
$script:SidLocalService = 'S-1-5-19'
$script:SidNetworkService = 'S-1-5-20'
$script:Services = @('SanaGtmPostgres', 'SanaGtmWeb', 'SanaGtmWorker', 'SanaGtmTunnel')

function Write-Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "   ok    $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host "   warn  $Text" -ForegroundColor Yellow }
function Write-Bad([string]$Text) { Write-Host "   FAIL  $Text" -ForegroundColor Red }
function Stop-WithError([string]$Text) { throw "SANA GTM: $Text" }

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-CurrentUserSid { return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }

# ------------------------------------------------------------------------------------------------
# layout and configuration files
# ------------------------------------------------------------------------------------------------

function Get-SanaLayout([string]$InstallDir, [string]$DataDir) {
    $i = [IO.Path]::GetFullPath($InstallDir); $d = [IO.Path]::GetFullPath($DataDir)
    return [ordered]@{
        InstallDir = $i; DataDir = $d
        App = Join-Path $i 'app'; Web = Join-Path $i 'web\dist'; Bin = Join-Path $i 'bin'
        Runtime = Join-Path $i 'runtime'
        Python = Join-Path $i 'runtime\python\python.exe'; PythonW = Join-Path $i 'runtime\python\pythonw.exe'
        PgBin = Join-Path $i 'runtime\pgsql\bin'; ServiceBin = Join-Path $i 'runtime\services'
        Cloudflared = Join-Path $i 'runtime\cloudflared\cloudflared.exe'
        Config = Join-Path $d 'config'; EnvFile = Join-Path $d 'config\sanagtm.env'
        PgPasswordFile = Join-Path $d 'config\postgres.superuser'
        TunnelTokenFile = Join-Path $d 'config\tunnel.token'; InstallInfo = Join-Path $d 'config\install.json'
        PgData = Join-Path $d 'pgdata'; Files = Join-Path $d 'files'; Results = Join-Path $d 'results'
        Run = Join-Path $d 'run'; Backups = Join-Path $d 'backups'; Logs = Join-Path $d 'logs'
        Tmp = Join-Path $d 'files\tmp'
    }
}

function Read-KeyValueFile([string]$Path) {
    $out = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Path)) { return $out }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#') -or $t.IndexOf('=') -lt 1) { continue }
        $k = $t.Substring(0, $t.IndexOf('=')).Trim(); $v = $t.Substring($t.IndexOf('=') + 1).Trim()
        if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) {
            $v = $v.Substring(1, $v.Length - 2)
        }
        $out[$k] = $v
    }
    return $out
}

function Protect-SanaPath([string]$Path, [string[]]$ReadSids = @(), [string[]]$ModifySids = @(), [switch]$Recurse) {
    # Replace inherited permissions with: SYSTEM + Administrators + the installing user (full),
    # plus the given service accounts. Locale-independent (SIDs).
    $grants = @("*$($script:SidSystem):(OI)(CI)F", "*$($script:SidAdmins):(OI)(CI)F", "*$(Get-CurrentUserSid):(OI)(CI)F")
    foreach ($s in $ModifySids) { $grants += "*${s}:(OI)(CI)M" }
    foreach ($s in $ReadSids) { $grants += "*${s}:(OI)(CI)RX" }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        $grants = $grants | ForEach-Object { $_ -replace '\(OI\)\(CI\)', '' }
    }
    $icaclsArgs = @($Path, '/inheritance:r', '/grant:r') + $grants
    $null = & icacls.exe @icaclsArgs 2>&1
    if ($LASTEXITCODE -ne 0) { Stop-WithError "could not set permissions on $Path" }
    if ($Recurse -and (Test-Path -LiteralPath $Path -PathType Container) -and
            @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue).Count -gt 0) {
        # Existing contents inherit exactly the folder's protection. (Granting the folder's
        # inheritable entries to files directly would leave them with an EMPTY access list.)
        $null = & icacls.exe (Join-Path $Path '*') /reset /T /C /Q 2>&1
        if ($LASTEXITCODE -ne 0) { Stop-WithError "could not set permissions inside $Path" }
    }
}

function Write-SecretFile([string]$Path, [string]$Content, [string[]]$ReadSids = @()) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    if (-not (Test-Path -LiteralPath $Path)) { [IO.File]::WriteAllText($Path, '') }
    Protect-SanaPath -Path $Path -ReadSids $ReadSids          # lock it down BEFORE the secret is written
    [IO.File]::WriteAllText($Path, $Content, (New-Object Text.UTF8Encoding($false)))
}

function New-RandomHex([int]$Bytes = 24) {
    $buf = New-Object byte[] $Bytes
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    return -join ($buf | ForEach-Object { $_.ToString('x2') })
}

function New-FernetKey {
    $buf = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    return [Convert]::ToBase64String($buf).Replace('+', '-').Replace('/', '_')
}

function ConvertFrom-SecureToPlain([Security.SecureString]$Secure) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Get-InstallInfo([string]$DataDir) {
    $p = Join-Path $DataDir 'config\install.json'
    if (-not (Test-Path -LiteralPath $p)) { Stop-WithError "SANA GTM is not installed with data in $DataDir (no config\install.json)" }
    return Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
}

# ------------------------------------------------------------------------------------------------
# running processes without leaking secrets
# ------------------------------------------------------------------------------------------------

function Invoke-Captured([string]$FilePath, [string[]]$Arguments = @(), [hashtable]$Environment = @{},
                        [string]$WorkingDirectory = '', [int]$TimeoutSeconds = 3600) {
    # Runs a process to completion; its environment gets $Environment (e.g. the app settings) so
    # secrets travel as environment variables, never as arguments. Returns exit code + output.
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ($Arguments | ForEach-Object { ConvertTo-CommandArgument $_ }) -join ' '
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    foreach ($k in $Environment.Keys) { $psi.EnvironmentVariables[$k] = [string]$Environment[$k] }
    $p = [Diagnostics.Process]::Start($psi)
    $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEndAsync(); $err = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($TimeoutSeconds * 1000)) { $p.Kill(); Stop-WithError "$([IO.Path]::GetFileName($FilePath)) timed out" }
    $p.WaitForExit()
    return [pscustomobject]@{ ExitCode = $p.ExitCode; StdOut = $out.Result; StdErr = $err.Result }
}

function ConvertTo-CommandArgument([string]$Value) {
    if ($Value -eq '') { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Get-AppEnvironment($Layout, [hashtable]$Extra = @{}) {
    $envmap = @{}
    $kv = Read-KeyValueFile $Layout.EnvFile
    foreach ($k in $kv.Keys) { $envmap[$k] = $kv[$k] }
    $envmap['PYTHONPATH'] = $Layout.App
    $envmap['PYTHONDONTWRITEBYTECODE'] = '1'
    $envmap['PYTHONUTF8'] = '1'
    foreach ($k in $Extra.Keys) { $envmap[$k] = $Extra[$k] }
    return $envmap
}

function Invoke-SanaPython($Layout, [string[]]$Arguments, [hashtable]$Extra = @{}, [int]$TimeoutSeconds = 3600) {
    return Invoke-Captured -FilePath $Layout.Python -Arguments $Arguments -Environment (Get-AppEnvironment $Layout $Extra) `
        -WorkingDirectory $Layout.App -TimeoutSeconds $TimeoutSeconds
}

function Get-AdminDatabaseUrl($Layout, [int]$PgPort) {
    $pw = ([IO.File]::ReadAllText($Layout.PgPasswordFile)).Trim()
    return "postgresql://postgres:$pw@127.0.0.1:$PgPort/postgres"
}

# ------------------------------------------------------------------------------------------------
# components: download / verify / extract
# ------------------------------------------------------------------------------------------------

function Get-VerifiedComponent($Spec, [string]$ComponentsDir, [string]$DownloadDir) {
    $candidates = @()
    if ($ComponentsDir) { $candidates += (Join-Path $ComponentsDir $Spec.file) }
    $candidates += (Join-Path $DownloadDir $Spec.file)
    foreach ($c in $candidates) {
        if ((Test-Path -LiteralPath $c) -and ((Get-FileHash -LiteralPath $c -Algorithm SHA256).Hash.ToLower() -eq $Spec.sha256)) {
            return $c
        }
    }
    if (-not (Test-Path -LiteralPath $DownloadDir)) { New-Item -ItemType Directory -Path $DownloadDir -Force | Out-Null }
    $target = Join-Path $DownloadDir $Spec.file
    Write-Host "   downloading $($Spec.file) ..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $Spec.url -OutFile $target -UseBasicParsing
    $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $Spec.sha256) {
        Remove-Item -LiteralPath $target -Force
        Stop-WithError "$($Spec.file) failed its SHA-256 check (expected $($Spec.sha256), got $hash) - not used"
    }
    return $target
}

function Expand-ZipEntries([string]$Zip, [string]$Prefix, [string]$Destination, [string[]]$Keep = @()) {
    # Extract entries under $Prefix (e.g. 'tools/') into $Destination, optionally only the listed
    # top-level folders below the prefix.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        foreach ($e in $archive.Entries) {
            if (-not $e.FullName.StartsWith($Prefix) -or $e.FullName.EndsWith('/')) { continue }
            $rel = $e.FullName.Substring($Prefix.Length)
            if ($Keep.Count -gt 0 -and -not ($Keep -contains ($rel.Split('/')[0]))) { continue }
            $out = Join-Path $Destination ($rel -replace '/', '\')
            $full = [IO.Path]::GetFullPath($out)
            if (-not $full.StartsWith([IO.Path]::GetFullPath($Destination))) { Stop-WithError "unsafe path in $Zip" }
            $dir = Split-Path -Parent $full
            if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            for ($attempt = 1; ; $attempt++) {
                # antivirus briefly locks freshly written executables: retry instead of failing
                try { [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $full, $true); break }
                catch [IO.IOException] { if ($attempt -ge 10) { throw }; Start-Sleep -Milliseconds (300 * $attempt) }
            }
        }
    } finally { $archive.Dispose() }
}

function Test-VcRuntime {
    $k = 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64'
    try { $v = Get-ItemProperty -Path $k -ErrorAction Stop; return ([int]$v.Installed -eq 1) } catch { }
    # app-local or system copies are enough for PostgreSQL
    foreach ($dll in @('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll')) {
        if (-not (Test-Path (Join-Path $env:WINDIR "System32\$dll"))) { return $false }
    }
    return $true
}

function Install-VcRuntime([string]$DownloadDir) {
    # Microsoft Visual C++ 2015-2022 Redistributable (x64), required by the PostgreSQL binaries.
    $target = Join-Path $DownloadDir 'vc_redist.x64.exe'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile $target -UseBasicParsing
    $sig = Get-AuthenticodeSignature -LiteralPath $target
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
        Remove-Item -LiteralPath $target -Force
        Stop-WithError 'the Visual C++ runtime download is not signed by Microsoft - not run'
    }
    $p = Start-Process -FilePath $target -ArgumentList '/install', '/quiet', '/norestart' -PassThru -Wait
    if (@(0, 1638, 3010) -notcontains $p.ExitCode) { Stop-WithError "Visual C++ runtime install failed ($($p.ExitCode))" }
}

# ------------------------------------------------------------------------------------------------
# PostgreSQL
# ------------------------------------------------------------------------------------------------

function Test-PgReady($Layout, [int]$PgPort) {
    $r = Invoke-Captured -FilePath (Join-Path $Layout.PgBin 'pg_isready.exe') -Arguments @('-h', '127.0.0.1', '-p', "$PgPort", '-t', '3')
    return ($r.ExitCode -eq 0)
}

function Start-PgDirect($Layout, [int]$PgPort) {
    # User mode: pg_ctl start. Not through a pipe: the server inherits pg_ctl's handles.
    $log = Join-Path $Layout.Logs 'postgres.log'
    $p = Start-Process -FilePath (Join-Path $Layout.PgBin 'pg_ctl.exe') -WindowStyle Hidden -PassThru `
        -ArgumentList @('-D', "`"$($Layout.PgData)`"", '-l', "`"$log`"", '-w', '-t', '90', 'start')
    $p.WaitForExit()
    if (-not (Test-PgReady $Layout $PgPort)) { Stop-WithError "PostgreSQL did not start (see $log)" }
}

function Stop-PgDirect($Layout) {
    $r = Invoke-Captured -FilePath (Join-Path $Layout.PgBin 'pg_ctl.exe') -Arguments @('-D', $Layout.PgData, '-m', 'fast', '-w', '-t', '90', 'stop')
    return ($r.ExitCode -eq 0)
}

function Initialize-PgCluster($Layout, [int]$PgPort, [string]$SuperPassword, [string]$AppPassword) {
    $pwfile = Join-Path $Layout.Config 'initdb.tmp'
    Write-SecretFile -Path $pwfile -Content $SuperPassword
    try {
        $r = Invoke-Captured -FilePath (Join-Path $Layout.PgBin 'initdb.exe') -Arguments @(
            '-D', $Layout.PgData, '-U', 'postgres', '--auth=scram-sha-256', "--pwfile=$pwfile", '--encoding=UTF8',
            '--locale=C', '--no-instructions')
        if ($r.ExitCode -ne 0) { Stop-WithError "initdb failed: $($r.StdErr)" }
    } finally { Remove-Item -LiteralPath $pwfile -Force -ErrorAction SilentlyContinue }
    $conf = Join-Path $Layout.PgData 'postgresql.conf'
    $tuning = @"

# --- SANA GTM (written by the installer) ---
listen_addresses = '127.0.0.1'
port = $PgPort
password_encryption = scram-sha-256
shared_buffers = 1GB
effective_cache_size = 3GB
work_mem = 16MB
maintenance_work_mem = 256MB
max_connections = 60
max_wal_size = 2GB
wal_compression = on
checkpoint_timeout = 15min
log_min_duration_statement = 2000
autovacuum_vacuum_scale_factor = 0.05
autovacuum_analyze_scale_factor = 0.05
"@
    [IO.File]::AppendAllText($conf, $tuning)
    # Only this machine, only password logins: never reachable from the network.
    $hba = @"
# SANA GTM: local connections only, SCRAM passwords.
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256
"@
    [IO.File]::WriteAllText((Join-Path $Layout.PgData 'pg_hba.conf'), $hba)
}

function Initialize-PgDatabase($Layout, [int]$PgPort, [string]$SuperPassword, [string]$AppPassword) {
    # Same roles as deploy/selfhost/postgres/initdb/10-sanagtm.sh; the password reaches psql as a
    # variable read from a protected file, never as SQL text or an argument.
    $sql = @'
\getenv app_password SANAGTM_APP_PASSWORD
SELECT format('CREATE ROLE sanagtm LOGIN PASSWORD %L', :'app_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sanagtm') \gexec
SELECT 'CREATE ROLE authenticated NOLOGIN NOINHERIT'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') \gexec
GRANT authenticated TO sanagtm;
SELECT 'CREATE DATABASE sanagtm OWNER sanagtm'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'sanagtm') \gexec
REVOKE ALL ON DATABASE sanagtm FROM PUBLIC;
'@
    $sqlFile = Join-Path $Layout.Config 'init.sql.tmp'
    Write-SecretFile -Path $sqlFile -Content $sql
    try {
        $envmap = @{ PGPASSWORD = $SuperPassword; PGHOST = '127.0.0.1'; PGPORT = "$PgPort"; PGUSER = 'postgres';
                     SANAGTM_APP_PASSWORD = $AppPassword }
        $psql = Join-Path $Layout.PgBin 'psql.exe'
        $r = Invoke-Captured -FilePath $psql -Arguments @('-v', 'ON_ERROR_STOP=1', '-d', 'postgres', '-f', $sqlFile) -Environment $envmap
        if ($r.ExitCode -ne 0) { Stop-WithError "database setup failed: $($r.StdErr)" }
        $r = Invoke-Captured -FilePath $psql -Arguments @('-v', 'ON_ERROR_STOP=1', '-d', 'sanagtm', '-c', 'REVOKE CREATE ON SCHEMA public FROM PUBLIC') -Environment $envmap
        if ($r.ExitCode -ne 0) { Stop-WithError "database setup failed: $($r.StdErr)" }
    } finally {
        Remove-Item -LiteralPath $sqlFile -Force -ErrorAction SilentlyContinue
    }
}

# ------------------------------------------------------------------------------------------------
# the application processes (user mode) and Windows services (service mode)
# ------------------------------------------------------------------------------------------------

function Get-AppCommands($Layout, $Info) {
    $envFile = $Layout.EnvFile
    return [ordered]@{
        web = @('-m', 'uvicorn', 'cloud.ops.webapp:create_app', '--factory', '--host', '127.0.0.1', '--port', "$($Info.WebPort)",
                '--env-file', $envFile, '--no-server-header')
        worker = @('-m', 'cloud.intel.tasks.worker', '--env-file', $envFile, '--concurrency', "$($Info.WorkerConcurrency)",
                   '--state-file', (Join-Path $Layout.Run 'worker-state.json'))
    }
}

function Start-UserProcess($Layout, [string]$Name, [string[]]$Arguments, [string]$Exe = '') {
    if (-not $Exe) { $Exe = $Layout.PythonW }
    $out = Join-Path $Layout.Logs "$Name.out.log"; $err = Join-Path $Layout.Logs "$Name.err.log"
    foreach ($f in @($out, $err)) {                 # roll at 10 MB, keep one previous file
        if ((Test-Path -LiteralPath $f) -and (Get-Item -LiteralPath $f).Length -gt 10MB) { Move-Item -LiteralPath $f "$f.1" -Force }
    }
    $quoted = $Arguments | ForEach-Object { ConvertTo-CommandArgument $_ }
    $p = Start-Process -FilePath $Exe -ArgumentList $quoted -WorkingDirectory $Layout.App -WindowStyle Hidden `
        -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    Set-Content -LiteralPath (Join-Path $Layout.Run "$Name.pid") -Value $p.Id -Encoding ascii
    return $p
}

function Start-NoConsoleProcess($Layout, [string]$Name, [string]$Exe, [string[]]$Arguments) {
    # A console program (cloudflared) without any window: CREATE_NO_WINDOW. Start-Process with
    # -WindowStyle Hidden is not enough on Windows 11, where Windows Terminal hosts new consoles.
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = ($Arguments | ForEach-Object { ConvertTo-CommandArgument $_ }) -join ' '
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true; $psi.WorkingDirectory = $Layout.Run
    $p = [Diagnostics.Process]::Start($psi)
    Set-Content -LiteralPath (Join-Path $Layout.Run "$Name.pid") -Value $p.Id -Encoding ascii
    return $p
}

function Get-UserProcess($Layout, [string]$Name) {
    $pidFile = Join-Path $Layout.Run "$Name.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    $procId = [int](Get-Content -LiteralPath $pidFile -Raw)
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($p -and $p.Path -and ($p.Path -like "$($Layout.Runtime)*")) { return $p }   # never someone else's PID
    return $null
}

function Stop-UserProcess($Layout, [string]$Name) {
    $p = Get-UserProcess $Layout $Name
    if ($p) { Stop-Process -Id $p.Id -Force; $p.WaitForExit(30000) | Out-Null }
    Remove-Item -LiteralPath (Join-Path $Layout.Run "$Name.pid") -Force -ErrorAction SilentlyContinue
}

function New-WinSwConfig($Layout, [string]$Id, [string]$Title, [string]$Executable, [string[]]$Arguments,
                         [string[]]$DependsOn, [int]$StopSeconds) {
    $argText = [Security.SecurityElement]::Escape((($Arguments | ForEach-Object { ConvertTo-CommandArgument $_ }) -join ' '))
    $deps = ($DependsOn | ForEach-Object { "  <depend>$_</depend>" }) -join "`r`n"
    $xml = @"
<service>
  <id>$Id</id>
  <name>$Title</name>
  <description>$Title (SANA GTM self-hosted). Managed with sanagtm.cmd.</description>
  <executable>$([Security.SecurityElement]::Escape($Executable))</executable>
  <arguments>$argText</arguments>
  <workingdirectory>$([Security.SecurityElement]::Escape($Layout.App))</workingdirectory>
  <startmode>Automatic</startmode>
$deps
  <onfailure action="restart" delay="10 sec"/>
  <onfailure action="restart" delay="30 sec"/>
  <onfailure action="restart" delay="60 sec"/>
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>$StopSeconds sec</stoptimeout>
  <logpath>$([Security.SecurityElement]::Escape($Layout.Logs))</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
  <env name="PYTHONUNBUFFERED" value="1"/>
  <env name="PYTHONDONTWRITEBYTECODE" value="1"/>
  <env name="PYTHONPATH" value="$([Security.SecurityElement]::Escape($Layout.App))"/>
</service>
"@
    if (-not (Test-Path -LiteralPath $Layout.ServiceBin)) { New-Item -ItemType Directory -Path $Layout.ServiceBin -Force | Out-Null }
    $exe = Join-Path $Layout.ServiceBin "$Id.exe"
    Copy-Item -LiteralPath (Join-Path $Layout.Runtime 'winsw\WinSW-x64.exe') -Destination $exe -Force
    [IO.File]::WriteAllText((Join-Path $Layout.ServiceBin "$Id.xml"), $xml, (New-Object Text.UTF8Encoding($false)))
    return $exe
}

function Install-SanaServices($Layout, $Info) {
    if (-not (Test-IsAdmin)) { Stop-WithError 'registering Windows services needs an administrator PowerShell' }
    $pgctl = Join-Path $Layout.PgBin 'pg_ctl.exe'
    $r = Invoke-Captured -FilePath $pgctl -Arguments @('register', '-N', 'SanaGtmPostgres', '-U', 'NT AUTHORITY\NetworkService',
        '-D', $Layout.PgData, '-S', 'auto', '-w')
    if ($r.ExitCode -ne 0) { Stop-WithError "could not register the PostgreSQL service: $($r.StdErr)" }
    $cmd = Get-AppCommands $Layout $Info
    $web = New-WinSwConfig $Layout 'SanaGtmWeb' 'SANA GTM web and API' $Layout.Python $cmd.web @('SanaGtmPostgres') 60
    $worker = New-WinSwConfig $Layout 'SanaGtmWorker' 'SANA GTM background worker' $Layout.Python $cmd.worker @('SanaGtmPostgres') 90
    $exes = @($web, $worker)
    if ($Info.Tunnel) {
        $exes += New-WinSwConfig $Layout 'SanaGtmTunnel' 'SANA GTM Cloudflare Tunnel' $Layout.Cloudflared `
            @('tunnel', '--no-autoupdate', 'run', '--token-file', $Layout.TunnelTokenFile) @('SanaGtmWeb') 30
    }
    foreach ($exe in $exes) {
        $r = Invoke-Captured -FilePath $exe -Arguments @('install')
        if ($r.ExitCode -ne 0) { Stop-WithError "could not install $([IO.Path]::GetFileName($exe)): $($r.StdOut) $($r.StdErr)" }
        $id = [IO.Path]::GetFileNameWithoutExtension($exe)
        # Least privilege: LocalService (no network credentials, no admin rights).
        $svc = Get-CimInstance -ClassName Win32_Service -Filter "Name='$id'"
        $res = Invoke-CimMethod -InputObject $svc -MethodName Change -Arguments @{
            StartName = 'NT AUTHORITY\LocalService'; StartPassword = '' }
        if ($res.ReturnValue -ne 0) { Stop-WithError "could not set the service account of $id ($($res.ReturnValue))" }
        $null = & sc.exe config $id start= delayed-auto
        if ($LASTEXITCODE -ne 0) { Stop-WithError "could not set delayed automatic start for $id" }
    }
    $null = & sc.exe config SanaGtmPostgres start= delayed-auto
    $null = & sc.exe failure SanaGtmPostgres reset= 3600 actions= restart/10000/restart/30000/restart/60000
}

function Uninstall-SanaServices($Layout) {
    foreach ($id in @('SanaGtmTunnel', 'SanaGtmWorker', 'SanaGtmWeb')) {
        $exe = Join-Path $Layout.ServiceBin "$id.exe"
        if (Get-Service -Name $id -ErrorAction SilentlyContinue) {
            Stop-Service -Name $id -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $exe) { $null = Invoke-Captured -FilePath $exe -Arguments @('uninstall') }
            else { $null = & sc.exe delete $id }
        }
    }
    if (Get-Service -Name 'SanaGtmPostgres' -ErrorAction SilentlyContinue) {
        Stop-Service -Name 'SanaGtmPostgres' -Force -ErrorAction SilentlyContinue
        $null = Invoke-Captured -FilePath (Join-Path $Layout.PgBin 'pg_ctl.exe') -Arguments @('unregister', '-N', 'SanaGtmPostgres')
    }
}

function Set-ServiceModePermissions($Layout) {
    # Services run as LocalService (web, worker, tunnel) and NetworkService (PostgreSQL).
    Protect-SanaPath -Path $Layout.PgData -ModifySids @($script:SidNetworkService) -Recurse
    foreach ($dir in @($Layout.Files, $Layout.Results, $Layout.Run, $Layout.Logs, $Layout.Backups)) {
        Protect-SanaPath -Path $dir -ModifySids @($script:SidLocalService, $script:SidNetworkService) -Recurse
    }
    Protect-SanaPath -Path $Layout.Config -ReadSids @($script:SidLocalService) -Recurse
}

function Register-UserLogonTask($Layout) {
    # No administrator needed: starts SANA GTM when THIS user signs in (service mode starts at boot).
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
        "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$($Layout.Bin)\sanagtm.ps1`" start")
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    Register-ScheduledTask -TaskName 'SANA GTM (self-hosted)' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
}

Export-ModuleMember -Function * -Variable Services
