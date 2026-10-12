<#
.SYNOPSIS
  Manage self-hosted SANA GTM on Windows.

.DESCRIPTION
  sanagtm start | stop | restart | status | health [-Quiet] | logs [web|worker|tunnel|postgres] [-Follow]
  sanagtm backup [-Label name] [-Offsite D:\folder] | backups
  sanagtm restore <dump> [-Live] [-ConfirmHostname host]
  sanagtm migrate dump-hosted <env file> | migrate import <dump> | migrate verify <manifest> | migrate verify-secrets
  sanagtm capacity | update -Package <zip|folder> | rollback-update | selftest
  sanagtm uninstall [-PurgeData] | version
  Service-mode installs need an Administrator prompt for start/stop/restart/update/uninstall.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Command = 'help',
    [Parameter(Position = 1)][string]$Target = '',
    [Parameter(Position = 2)][string]$Target2 = '',
    [string]$Label = 'manual',
    [string]$Offsite = '',
    [string]$Package = '',
    [string]$ConfirmHostname = '',
    [int]$Tail = 100,
    [switch]$Follow,
    [switch]$Live,
    [switch]$Quiet,
    [switch]$PurgeData
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'lib\SanaGtm.psm1') -Force -DisableNameChecking

$InstallDir = Split-Path -Parent $PSScriptRoot
$locFile = Join-Path $PSScriptRoot 'sanagtm.location'
if ($Command -notin @('help', 'version')) {
    if (-not (Test-Path -LiteralPath $locFile)) { Stop-WithError "not installed (missing $locFile)" }
}
$DataDir = if (Test-Path -LiteralPath $locFile) { (Get-Content -LiteralPath $locFile -Raw).Trim() } else { '' }
$L = Get-SanaLayout $InstallDir $(if ($DataDir) { $DataDir } else { $InstallDir })
$Info = $null
if ($DataDir -and (Test-Path -LiteralPath $L.InstallInfo)) { $Info = Get-InstallInfo $DataDir }
$ServiceMode = ($Info -ne $null -and $Info.Mode -eq 'Service')
$BackupMaxAgeHours = 36

function Need-Admin([string]$What) {
    if ($ServiceMode -and -not (Test-IsAdmin)) { Stop-WithError "$What needs an Administrator PowerShell (service-mode install)" }
}

function Invoke-Py([string[]]$Arguments, [hashtable]$Extra = @{}, [switch]$Show) {
    $r = Invoke-SanaPython $L $Arguments $Extra
    if ($Show) { if ($r.StdOut) { Write-Host $r.StdOut.TrimEnd() }; if ($r.ExitCode -ne 0 -and $r.StdErr) { Write-Host $r.StdErr.TrimEnd() -ForegroundColor Yellow } }
    return $r
}

function Invoke-Prepare {
    $r = Invoke-Py @('-m', 'cloud.ops.selfhost', 'prepare') @{ SANAGTM_DB_WAIT_SECONDS = '90' }
    if ($r.ExitCode -ne 0) { Stop-WithError "database not ready: $($r.StdErr.Trim())" }
}

function Start-Sana([switch]$WithoutWorker) {
    if ($ServiceMode) {
        Need-Admin 'start'
        Start-Service -Name SanaGtmPostgres
        Invoke-Prepare
        Start-Service -Name SanaGtmWeb
        if (-not $WithoutWorker) { Start-Service -Name SanaGtmWorker }
        if ($Info.Tunnel) { Start-Service -Name SanaGtmTunnel }
    } else {
        if (-not (Test-PgReady $L $Info.PgPort)) { Start-PgDirect $L $Info.PgPort }
        Invoke-Prepare
        $cmd = Get-AppCommands $L $Info
        if (-not (Get-UserProcess $L 'web')) { $null = Start-UserProcess $L 'web' $cmd.web }
        if (-not $WithoutWorker -and -not (Get-UserProcess $L 'worker')) { $null = Start-UserProcess $L 'worker' $cmd.worker }
        if ($Info.Tunnel -and -not (Get-UserProcess $L 'tunnel')) {
            $null = Start-NoConsoleProcess $L 'tunnel' $L.Cloudflared @('tunnel', '--no-autoupdate', '--logfile',
                (Join-Path $L.Logs 'tunnel.log'), 'run', '--token-file', $L.TunnelTokenFile)
        }
    }
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        try { $w = Invoke-WebRequest -Uri "http://127.0.0.1:$($Info.WebPort)/healthz" -UseBasicParsing -TimeoutSec 5; if ($w.StatusCode -eq 200) { break } } catch { }
        Start-Sleep -Seconds 2
    }
    Write-Host "SANA GTM started (http://127.0.0.1:$($Info.WebPort))"
}

function Stop-Sana([switch]$KeepDatabase) {
    if ($ServiceMode) {
        Need-Admin 'stop'
        foreach ($s in @('SanaGtmTunnel', 'SanaGtmWorker', 'SanaGtmWeb')) {
            if (Get-Service -Name $s -ErrorAction SilentlyContinue) { Stop-Service -Name $s -Force -ErrorAction SilentlyContinue }
        }
        if (-not $KeepDatabase) { Stop-Service -Name SanaGtmPostgres -Force -ErrorAction SilentlyContinue }
    } else {
        foreach ($n in @('tunnel', 'worker', 'web')) { Stop-UserProcess $L $n }
        if (-not $KeepDatabase) { $null = Stop-PgDirect $L }
    }
    if ($KeepDatabase) { Write-Host 'SANA GTM stopped (database still running)' } else { Write-Host 'SANA GTM stopped' }
}

function Get-NewestBackup {
    Get-ChildItem -LiteralPath $L.Backups -Filter '*.dump' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
}

function Show-Status {
    Write-Host "SANA GTM  mode=$($Info.Mode)  web=http://127.0.0.1:$($Info.WebPort)  public=https://$($Info.Hostname)"
    Write-Host "program  $($L.InstallDir)"; Write-Host "data     $($L.DataDir)"
    if ($ServiceMode) {
        Get-Service -Name SanaGtmPostgres, SanaGtmWeb, SanaGtmWorker, SanaGtmTunnel -ErrorAction SilentlyContinue |
            Format-Table Name, Status, StartType -AutoSize | Out-String | Write-Host
    } else {
        Write-Host ("postgres {0}" -f $(if (Test-PgReady $L $Info.PgPort) { 'running' } else { 'stopped' }))
        foreach ($n in @('web', 'worker', 'tunnel')) {
            $p = Get-UserProcess $L $n
            Write-Host ("{0,-8} {1}" -f $n, $(if ($p) { "running (pid $($p.Id))" } else { 'stopped' }))
        }
    }
    $r = Invoke-Py @('-m', 'cloud.ops.selfhost', 'info')
    if ($r.ExitCode -eq 0) { Write-Host "database $($r.StdOut.Trim())" }
    $b = Get-NewestBackup
    Write-Host ("backup   {0}" -f $(if ($b) { "$($b.Name) ($([math]::Round(((Get-Date) - $b.LastWriteTime).TotalHours, 1)) h ago)" } else { 'none yet' }))
}

function Test-Health {
    $results = New-Object Collections.Generic.List[object]
    function Add($name, [bool]$ok) { $results.Add([pscustomobject]@{ check = $name; ok = $ok }) }
    Add 'PostgreSQL ready (127.0.0.1)' (Test-PgReady $L $Info.PgPort)
    $web = $false; try { $web = (Invoke-WebRequest -Uri "http://127.0.0.1:$($Info.WebPort)/healthz" -UseBasicParsing -TimeoutSec 10).StatusCode -eq 200 } catch { }
    Add 'dashboard /healthz' $web
    Add 'api /api/v1/health' ((Invoke-Py @('-m', 'cloud.ops.selfhost', 'health', 'api')).ExitCode -eq 0)
    Add 'worker heartbeat + database' ((Invoke-Py @('-m', 'cloud.ops.selfhost', 'health', 'worker')).ExitCode -eq 0)
    if ($Info.Tunnel) {
        $tun = if ($ServiceMode) { (Get-Service SanaGtmTunnel -ErrorAction SilentlyContinue).Status -eq 'Running' } else { [bool](Get-UserProcess $L 'tunnel') }
        Add 'Cloudflare Tunnel running' $tun
        $pub = $false; try { $pub = (Invoke-WebRequest -Uri "https://$($Info.Hostname)/api/v1/health" -UseBasicParsing -TimeoutSec 15).StatusCode -eq 200 } catch { }
        Add "public https://$($Info.Hostname)/api/v1/health" $pub
    }
    $b = Get-NewestBackup
    $age = if ($b) { ((Get-Date) - $b.LastWriteTime).TotalHours } else { 1e9 }
    Add ("backup newer than {0} h" -f $BackupMaxAgeHours) ($age -lt $BackupMaxAgeHours)
    $root = [IO.Path]::GetPathRoot($L.DataDir).TrimEnd('\')
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$root'"
    $freePct = [math]::Round(100 * $disk.FreeSpace / $disk.Size)
    Add "data drive $freePct% free (>= 20%)" ($freePct -ge 20)
    $failed = @($results | Where-Object { -not $_.ok }).Count
    $json = @{ checked_at = (Get-Date).ToUniversalTime().ToString('s') + 'Z'; ok = ($failed -eq 0); checks = $results }
    [IO.File]::WriteAllText((Join-Path $L.Run 'health.json'), ($json | ConvertTo-Json -Depth 4))
    if (-not ($Quiet -and $failed -eq 0)) {
        foreach ($c in $results) { if ($c.ok) { Write-Ok $c.check } else { Write-Bad $c.check } }
    }
    return ($failed -eq 0)
}

function Show-Logs([string]$Name) {
    if (-not $Name) {
        Get-ChildItem -LiteralPath $L.Logs -File | Sort-Object LastWriteTime -Descending | Format-Table Name, Length, LastWriteTime -AutoSize
        return
    }
    $map = @{
        web = @('web.err.log', 'SanaGtmWeb.err.log', 'SanaGtmWeb.out.log', 'web.out.log')
        worker = @('worker.err.log', 'SanaGtmWorker.err.log', 'SanaGtmWorker.out.log', 'worker.out.log')
        tunnel = @('tunnel.log', 'SanaGtmTunnel.err.log', 'SanaGtmTunnel.out.log')
        postgres = @('postgres.log')
    }
    if (-not $map.ContainsKey($Name)) { Stop-WithError 'logs: web | worker | tunnel | postgres' }
    $file = $map[$Name] | ForEach-Object { Join-Path $L.Logs $_ } | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $file -and $Name -eq 'postgres') {
        $file = Get-ChildItem -LiteralPath (Join-Path $L.PgData 'log') -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $file) { Write-Host "no $Name log yet"; return }
    if ($Follow) { Get-Content -LiteralPath $file -Tail $Tail -Wait } else { Get-Content -LiteralPath $file -Tail $Tail }
}

function Invoke-Backup([string]$Name) {
    if ($Name -notmatch '^[a-z0-9-]+$') { Stop-WithError 'label: lowercase letters, digits and dashes' }
    $target = @('--local-only')
    if ($Offsite) { $target = @('--offsite', $Offsite) }
    $r = Invoke-Py (@('deploy/ops/backup/sana_backup.py', 'backup', '--pg-bin', $L.PgBin, '--out', $L.Backups,
                      '--label', "sanagtm-$Name") + $target) -Show
    if ($r.ExitCode -ne 0) { Stop-WithError 'database backup failed' }
    $ts = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $stage = Join-Path $L.Backups "files_$ts"
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    foreach ($d in @('files', 'results')) {
        $src = Join-Path $L.DataDir $d
        if (Test-Path -LiteralPath $src) { robocopy $src (Join-Path $stage $d) /E /XD tmp /NFL /NDL /NJH /NJS /NP | Out-Null }
    }
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath "$stage.zip" -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stage -Recurse -Force
    # The configuration holds SECRETS: kept in a restricted folder, not encrypted, never sent anywhere.
    $cfgDir = Join-Path $L.Backups 'config'
    if (-not (Test-Path -LiteralPath $cfgDir)) { New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null; Protect-SanaPath -Path $cfgDir }
    Compress-Archive -Path (Join-Path $L.Config '*') -DestinationPath (Join-Path $cfgDir "config_$ts.zip") -Force
    $null = Invoke-Py @('deploy/ops/backup/sana_backup.py', 'prune', '--dir', $L.Backups, '--daily', '14', '--weekly', '8', '--monthly', '6') -Show
    foreach ($pair in @(@($L.Backups, 'files_*.zip', 14), @($cfgDir, 'config_*.zip', 30))) {
        Get-ChildItem -LiteralPath $pair[0] -Filter $pair[1] | Sort-Object LastWriteTime -Descending | Select-Object -Skip $pair[2] |
            Remove-Item -Force
    }
}

function Get-DumpPath([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { Stop-WithError "no such dump: $Path" }
    $full = (Resolve-Path -LiteralPath $Path).Path
    if (-not (Test-Path -LiteralPath ($full -replace '\.dump$', '.manifest.json'))) { Stop-WithError 'the manifest (.manifest.json) must sit next to the dump' }
    return $full
}

function Invoke-RestoreTest([string]$Dump) {
    $r = Invoke-Py @('deploy/ops/backup/sana_backup.py', 'restore-test', '--dump', $Dump, '--pg-bin', $L.PgBin,
                     '--server-url-env', 'SANAGTM_ADMIN_DATABASE_URL') @{ SANAGTM_ADMIN_DATABASE_URL = (Get-AdminDatabaseUrl $L $Info.PgPort) } -Show
    if ($r.ExitCode -ne 0) { Stop-WithError 'restore test FAILED' }
}

function Invoke-RestoreLive([string]$Dump) {
    $r = Invoke-Py @('deploy/ops/backup/sana_backup.py', 'restore', '--dump', $Dump, '--pg-bin', $L.PgBin,
                     '--server-url-env', 'SANAGTM_ADMIN_DATABASE_URL', '--database', 'sanagtm', '--owner', 'sanagtm',
                     '--replace', '--confirm', 'sanagtm') @{ SANAGTM_ADMIN_DATABASE_URL = (Get-AdminDatabaseUrl $L $Info.PgPort) } -Show
    if ($r.ExitCode -ne 0) { Stop-WithError 'restore FAILED (the safety backup taken just before is in the backups folder)' }
    $r = Invoke-Py @('-m', 'cloud.ops.selfhost', 'stamp-restored') -Show
    if ($r.ExitCode -ne 0) { Stop-WithError 'could not stamp the restored database' }
}

function Confirm-Hostname([string]$Why) {
    $typed = $ConfirmHostname
    if (-not $typed) { $typed = Read-Host "$Why Type the hostname ($($Info.Hostname)) to continue" }
    if ($typed -ne $Info.Hostname) { Stop-WithError 'not confirmed' }
}

function Get-WorkspaceCount {
    $r = Invoke-Py @('-c', "import os,psycopg;c=psycopg.connect(os.environ['CAREERCLOUD_DATABASE_URL']);print(c.execute(""select count(*) from careercloud.workspaces"").fetchone()[0] if c.execute(""select to_regclass('careercloud.workspaces')"").fetchone()[0] else 0)")
    if ($r.ExitCode -ne 0) { Stop-WithError 'could not read the database' }
    return [int]$r.StdOut.Trim()
}

function Invoke-Update([string]$Source) {
    Need-Admin 'update'
    if (-not $Source) { Stop-WithError 'update -Package <SANA-GTM-Windows-Installer.zip or its unzipped folder>' }
    $src = $Source
    if ($Source.EndsWith('.zip')) {
        $src = Join-Path $env:TEMP ("sanagtm-update-" + (New-RandomHex 4))
        Expand-Archive -LiteralPath $Source -DestinationPath $src -Force
        $inner = Get-ChildItem -LiteralPath $src -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'Install-SanaGtm.ps1') } | Select-Object -First 1
        if ($inner) { $src = $inner.FullName }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $src 'app\cloud'))) { Stop-WithError "$Source is not a SANA GTM package" }
    Write-Step 'backup before updating'; Invoke-Backup 'pre-update'
    Stop-Sana -KeepDatabase
    foreach ($part in @('app', 'web', 'bin')) {
        $cur = Join-Path $L.InstallDir $part; $prev = "$cur.previous"
        if (Test-Path -LiteralPath $prev) { Remove-Item -LiteralPath $prev -Recurse -Force }
        Move-Item -LiteralPath $cur -Destination $prev
        Copy-Item -LiteralPath (Join-Path $src $part) -Destination $cur -Recurse -Force
    }
    Set-Content -LiteralPath (Join-Path $L.Bin 'sanagtm.location') -Value $L.DataDir -Encoding ascii
    foreach ($f in @('components.json', 'requirements-lock.txt', 'README-WINDOWS.md', 'PACKAGE-INFO.json')) {
        if (Test-Path -LiteralPath (Join-Path $src $f)) { Copy-Item -LiteralPath (Join-Path $src $f) -Destination $L.InstallDir -Force }
    }
    $r = Invoke-Captured -FilePath $L.Python -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '-q',
        '--no-warn-script-location', '-r', (Join-Path $L.InstallDir 'requirements-lock.txt')) -TimeoutSeconds 1800
    if ($r.ExitCode -ne 0) { Stop-WithError "package install failed; run: sanagtm rollback-update  ($($r.StdErr.Trim()))" }
    Start-Sana
    if (-not (Test-Health)) { Write-Warn2 'not healthy after the update. To go back: sanagtm rollback-update' }
}

function Invoke-RollbackUpdate {
    Need-Admin 'rollback-update'
    foreach ($part in @('app', 'web', 'bin')) {
        if (-not (Test-Path -LiteralPath (Join-Path $L.InstallDir "$part.previous"))) { Stop-WithError 'no previous version to roll back to' }
    }
    Stop-Sana -KeepDatabase
    foreach ($part in @('app', 'web', 'bin')) {
        $cur = Join-Path $L.InstallDir $part
        Remove-Item -LiteralPath $cur -Recurse -Force
        Move-Item -LiteralPath "$cur.previous" -Destination $cur
    }
    Set-Content -LiteralPath (Join-Path $L.Bin 'sanagtm.location') -Value $L.DataDir -Encoding ascii
    Write-Warn2 'program files rolled back. If the update applied a migration, also restore the pre-update backup: sanagtm restore <...pre-update...dump> -Live'
    Start-Sana
}

switch ($Command.ToLower()) {
    'start' { Start-Sana }
    'stop' { Stop-Sana }
    'restart' { Stop-Sana -KeepDatabase; Start-Sana }
    'status' { Show-Status }
    'health' { if (-not (Test-Health)) { exit 1 } }
    'logs' { Show-Logs $Target }
    'backup' { Invoke-Backup $Label }
    'backups' {
        Get-ChildItem -LiteralPath $L.Backups -Filter '*.dump' | Sort-Object LastWriteTime -Descending |
            Format-Table Name, @{ n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } }, LastWriteTime -AutoSize
    }
    'restore' {
        $dump = Get-DumpPath $Target
        if (-not $Live) { Invoke-RestoreTest $dump; break }
        Confirm-Hostname 'This REPLACES the live SANA GTM database.'
        Need-Admin 'restore -Live'
        Invoke-Backup 'pre-restore'
        Stop-Sana -KeepDatabase
        Invoke-RestoreLive $dump
        Start-Sana
    }
    'migrate' {
        switch ($Target) {
            'import' {
                $dump = Get-DumpPath $Target2
                if ((Get-WorkspaceCount) -ne 0) { Stop-WithError 'this install already has data; migrate import is for a NEW install (use restore -Live deliberately)' }
                Need-Admin 'migrate import'
                Write-Step '1/4 restore test into a scratch database'; Invoke-RestoreTest $dump
                Write-Step '2/4 load into the live database (web and worker stopped)'; Stop-Sana -KeepDatabase; Invoke-RestoreLive $dump
                Write-Step '3/4 provider credentials readable with this secrets key?'
                $null = Invoke-Py @('-m', 'cloud.ops.selfhost', 'verify-secrets') -Show
                Write-Step '4/4 starting the dashboard; the WORKER STAYS OFF until you run: sanagtm start'
                Start-Sana -WithoutWorker
                Write-Host "Imported. Check your data, then: sanagtm migrate verify $($dump -replace '\.dump$', '.manifest.json')"
            }
            'verify' {
                if (-not (Test-Path -LiteralPath $Target2)) { Stop-WithError 'migrate verify <manifest.json>' }
                $r = Invoke-Py @('deploy/ops/backup/sana_backup.py', 'verify', '--manifest', (Resolve-Path -LiteralPath $Target2).Path) -Show
                if ($r.ExitCode -ne 0) { exit 1 }
            }
            'verify-secrets' { $r = Invoke-Py @('-m', 'cloud.ops.selfhost', 'verify-secrets') -Show; if ($r.ExitCode -ne 0) { exit 1 } }
            'dump-hosted' {
                # Read-only backup of the HOSTED database (e.g. Supabase) with the bundled PostgreSQL 17
                # tools. Its URL is read from -Target2 (an env file with CAREERCLOUD_DATABASE_URL=...),
                # never from the command line.
                if (-not $Target2 -or -not (Test-Path -LiteralPath $Target2)) { Stop-WithError 'migrate dump-hosted <env file with CAREERCLOUD_DATABASE_URL>' }
                $r = Invoke-Py @('deploy/ops/backup/sana_backup.py', 'backup', '--env-file', (Resolve-Path -LiteralPath $Target2).Path,
                                 '--pg-bin', $L.PgBin, '--out', $L.Backups, '--label', 'supabase-final', '--local-only') -Show
                if ($r.ExitCode -ne 0) { Stop-WithError 'backup of the hosted database failed' }
            }
            default { Stop-WithError 'migrate dump-hosted <env file> | import <dump> | verify <manifest> | verify-secrets' }
        }
    }
    'capacity' {
        $r = Invoke-Py @('-m', 'cloud.ops.selfhost', 'info') -Show
        foreach ($d in @('pgdata', 'files', 'results', 'backups', 'logs')) {
            $p = Join-Path $L.DataDir $d
            $mb = (Get-ChildItem -LiteralPath $p -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB
            Write-Host ("{0,-8} {1,10:N1} MB" -f $d, $mb)
        }
        $root = [IO.Path]::GetPathRoot($L.DataDir).TrimEnd('\')
        $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$root'"
        Write-Host ("drive {0}  {1:N1} GB free of {2:N1} GB" -f $root, ($disk.FreeSpace / 1GB), ($disk.Size / 1GB))
        if ($r.ExitCode -eq 0) {
            $db = ($r.StdOut.Trim() | ConvertFrom-Json).database_mb
            Add-Content -LiteralPath (Join-Path $L.Run 'capacity.csv') -Value ("{0},{1},{2}" -f (Get-Date -Format 'yyyy-MM-dd'), $db, [math]::Round($disk.FreeSpace / 1GB, 1))
        }
    }
    'update' { Invoke-Update $Package }
    'rollback-update' { Invoke-RollbackUpdate }
    'selftest' {
        # The platform test suite on throwaway cc_test_* databases of this PostgreSQL.
        $clean = @{ CAREERCLOUD_TEST_DATABASE_URL = (Get-AdminDatabaseUrl $L $Info.PgPort); PYTHONPATH = $L.App; TMP = $L.Tmp; TEMP = $L.Tmp }
        $r = Invoke-Captured -FilePath $L.Python -Arguments @('-m', 'unittest', 'discover', '-s', 'cloud/tests', '-t', '.') `
            -Environment $clean -WorkingDirectory $L.App -TimeoutSeconds 7200
        $summary = ($r.StdErr -split "`n") | Where-Object { $_ -match '^(Ran |OK|FAILED)' }
        $summary | ForEach-Object { Write-Host $_ }
        if ($r.ExitCode -ne 0) { exit 1 }
    }
    'uninstall' {
        Need-Admin 'uninstall'
        if ($PurgeData) {
            $b = Get-NewestBackup
            if (-not $b -or ((Get-Date) - $b.LastWriteTime).TotalHours -ge 24) { Stop-WithError 'no backup from the last 24 h: take one (sanagtm backup) and copy it off this machine first' }
            $typed = $ConfirmHostname
            if (-not $typed) { $typed = Read-Host "PERMANENTLY deletes $($L.DataDir). Type 'DELETE $($Info.Hostname)'" } else { $typed = "DELETE $typed" }
            if ($typed -ne "DELETE $($Info.Hostname)") { Stop-WithError 'not confirmed' }
        }
        Stop-Sana
        if ($ServiceMode) { Uninstall-SanaServices $L }
        else { Unregister-ScheduledTask -TaskName 'SANA GTM (self-hosted)' -Confirm:$false -ErrorAction SilentlyContinue }
        Set-Location -LiteralPath $env:TEMP
        for ($i = 1; $i -le 10 -and (Test-Path -LiteralPath $L.InstallDir); $i++) {
            # processes may need a moment to release program files after stopping
            Remove-Item -LiteralPath $L.InstallDir -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $L.InstallDir) { Start-Sleep -Seconds 2 }
        }
        if (Test-Path -LiteralPath $L.InstallDir) { Write-Warn2 "some program files are still in use: delete $($L.InstallDir) after a restart" }
        if ($PurgeData) { Remove-Item -LiteralPath $L.DataDir -Recurse -Force; Write-Host 'program and data deleted' }
        else { Write-Host "program removed; data kept in $($L.DataDir) (reinstall with -ReuseData to continue)" }
    }
    'version' {
        $p = Join-Path $InstallDir 'PACKAGE-INFO.json'
        if (Test-Path -LiteralPath $p) { Get-Content -LiteralPath $p -Raw } else { 'unknown' }
    }
    default { Get-Help $PSCommandPath -Detailed | Out-String | Write-Host }
}
