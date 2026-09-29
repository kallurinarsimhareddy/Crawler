<#
  SANA GTM startup manager for this Windows PC.

    sana-gtm.ps1 -Action run        the supervisor (what the scheduled task runs; hidden, never exits)
    sana-gtm.ps1 -Action start      start the supervisor (via the scheduled task when installed)
    sana-gtm.ps1 -Action stop       stop the supervisor, then the API, worker and tunnel
    sana-gtm.ps1 -Action status     PostgreSQL / API / Worker / Tunnel / Overall
    sana-gtm.ps1 -Action install    register the "SANA GTM Auto Start" task (at logon)
    sana-gtm.ps1 -Action uninstall  remove that task

  The supervisor checks the database (the staging Supabase PostgreSQL the API is
  configured with), starts the API and waits for /api/v1/health, starts the
  platform worker, starts the Cloudflare quick tunnel (the same command as
  before: cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8100), and
  then restarts whatever dies. A process that is already running is adopted,
  never duplicated.

  A quick tunnel gets a NEW public URL whenever cloudflared restarts (so after
  every reboot). That is handled separately from starting the services: every 5
  minutes the supervisor runs sync-frontend.ps1 -Mode sync -Auto, which compares
  the tunnel URL with the URL the LIVE frontend calls and, only when they differ
  and all its guards pass (stable tunnel, lock, per-day and per-URL limits),
  rebuilds and redeploys the frontend once. Create logs\sana-gtm\frontend-autosync.off
  to turn that off.

  Logs: <repo>\logs\sana-gtm\. No password, key or token is ever logged: the
  env files are read only by the API, the worker and healthcheck.py.
#>
param(
    [ValidateSet("run", "start", "stop", "status", "install", "uninstall")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"

$Root        = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$LogDir      = Join-Path $Root "logs\sana-gtm"
$Python      = Join-Path $Root "cloud\.venv\Scripts\python.exe"
$ApiEnv      = Join-Path $Root "cloud\api\.env.sana-cloud"
$WorkerEnv   = Join-Path $Root "cloud\worker\.env.sana-cloud"
$WebEnv      = Join-Path $Root "cloud\web\.env.staging.local"
$HealthPy    = Join-Path $PSScriptRoot "healthcheck.py"
$SyncPs1     = Join-Path $PSScriptRoot "sync-frontend.ps1"
$Launcher    = Join-Path $PSScriptRoot "launch_hidden.py"
$Pythonw     = Join-Path $Root "cloud\.venv\Scripts\pythonw.exe"
$Cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$TaskName    = "SANA GTM Auto Start"
$ApiPort     = 8100
$ApiHealth   = "http://127.0.0.1:$ApiPort/api/v1/health"
$StopFlag    = Join-Path $LogDir "stop.flag"
$UrlFile     = Join-Path $LogDir "tunnel-url.txt"
$ManagerLog  = Join-Path $LogDir "manager.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$env:PYTHONUNBUFFERED = "1"   # service logs are written as they happen

# --- helpers -------------------------------------------------------------------------------

function Write-Log([string]$Message, [string]$Level = "INFO") {
    $line = "{0} {1,-5} {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    try {
        if ((Test-Path $ManagerLog) -and (Get-Item $ManagerLog).Length -gt 5MB) {
            Move-Item -Force $ManagerLog "$ManagerLog.1"
        }
        Add-Content -Path $ManagerLog -Value $line -Encoding UTF8
    } catch { }
    if ($Action -ne "run") { Write-Host $line }
}

function Get-Procs([string]$Name, [string[]]$Patterns) {
    @(Get-CimInstance Win32_Process -Filter "Name='$Name'" -ErrorAction SilentlyContinue | Where-Object {
        $cmd = $_.CommandLine
        if (-not $cmd) { return $false }
        foreach ($p in $Patterns) { if ($cmd -notmatch $p) { return $false } }
        return $true
    })
}

# The venv python.exe is a launcher that starts the real interpreter as a child
# with the same command line; the top-level process is the one we manage.
function Get-Roots($procs) {
    $ids = @($procs | ForEach-Object { $_.ProcessId })
    @($procs | Where-Object { $ids -notcontains $_.ParentProcessId })
}

function Get-ApiProcs    { Get-Roots (Get-Procs "python.exe" @("uvicorn", "cloud\.api\.main:app", "--port $ApiPort")) }
function Get-WorkerProcs { Get-Roots (Get-Procs "python.exe" @("cloud\.intel\.tasks\.worker", "\.env\.sana-cloud")) }
function Get-TunnelProcs { Get-Procs "cloudflared.exe" @("tunnel", "--url\s+http://127\.0\.0\.1:$ApiPort") }
function Get-ManagerProcs {
    @(Get-Procs "powershell.exe" @("sana-gtm\.ps1", "-Action\s+run") | Where-Object { $_.ProcessId -ne $PID })
}

function Stop-Tree($proc) {
    if ($proc) { & taskkill.exe /PID $proc.ProcessId /T /F 2>&1 | Out-Null }
}

function Rotate([string]$Path) {
    if (Test-Path $Path) { Move-Item -Force $Path "$Path.prev" }
}

function Test-Http([string]$Url, [int]$TimeoutSec = 10) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Get-Health {
    try {
        $json = & $Python $HealthPy $WorkerEnv 2>$null
        return ($json | Select-Object -Last 1 | ConvertFrom-Json)
    } catch { return $null }
}

function Get-FrontendApiUrl {
    # Only the public API URL is read from this file.
    if (-not (Test-Path $WebEnv)) { return "" }
    $line = Get-Content $WebEnv | Where-Object { $_ -match '^\s*VITE_API_URL\s*=' } | Select-Object -First 1
    if (-not $line) { return "" }
    return ($line -replace '^\s*VITE_API_URL\s*=\s*', '').Trim().TrimEnd('/')
}

function Get-TunnelUrl {
    if (Test-Path $UrlFile) {
        $u = (Get-Content $UrlFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($u) { return $u.Trim() }
    }
    return ""
}

function Find-TunnelUrl([string[]]$Files) {
    foreach ($f in $Files) {
        if (Test-Path $f) {
            $m = Select-String -Path $f -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches -ErrorAction SilentlyContinue |
                Select-Object -Last 1
            if ($m) { return $m.Matches[-1].Value }
        }
    }
    return ""
}

# Every process the supervisor starts goes through launch_hidden.py: CREATE_NO_WINDOW,
# stdin NUL, stdout/stderr to log files. Start-Process -WindowStyle Hidden is NOT used:
# it creates a normal console and hides it afterwards, and Windows 11 hands that console
# to Windows Terminal, which shows it anyway.
function Start-Hidden([string]$FilePath, [string[]]$Arguments, [string]$StdOut = "", [string]$StdErr = "") {
    $la = @($Launcher, "--cwd", $Root)
    if ($StdOut) { $la += @("--stdout", $StdOut) }
    if ($StdErr) { $la += @("--stderr", $StdErr) }
    $out = & $Python @la -- $FilePath @Arguments 2>&1
    $procId = 0
    if (-not [int]::TryParse(([string]($out | Select-Object -Last 1)).Trim(), [ref]$procId)) {
        throw "launch_hidden.py failed for ${FilePath}: $out"
    }
    return $procId
}

# --- starting services ------------------------------------------------------------------------

function Start-Api {
    Rotate (Join-Path $LogDir "api.out.log"); Rotate (Join-Path $LogDir "api.err.log")
    $procId = Start-Hidden $Python @("-m", "uvicorn", "cloud.api.main:app", "--host", "127.0.0.1", "--port", "$ApiPort",
        "--env-file", $ApiEnv, "--proxy-headers") (Join-Path $LogDir "api.out.log") (Join-Path $LogDir "api.err.log")
    Write-Log "API started (pid $procId, no window)"
}

function Start-Worker {
    Rotate (Join-Path $LogDir "worker.out.log"); Rotate (Join-Path $LogDir "worker.err.log")
    # The same command run-sanagtm-worker.bat runs, without the .bat / cmd.exe in between.
    $procId = Start-Hidden $Python @("-m", "cloud.intel.tasks.worker", "--env-file", $WorkerEnv) `
        (Join-Path $LogDir "worker.out.log") (Join-Path $LogDir "worker.err.log")
    Write-Log "Worker started (pid $procId, no window)"
}

function Start-Tunnel {
    $log = Join-Path $LogDir "tunnel.log"
    Rotate $log; Rotate (Join-Path $LogDir "tunnel.out.log")
    $old = Get-TunnelUrl
    $procId = Start-Hidden $Cloudflared @("tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:$ApiPort") `
        (Join-Path $LogDir "tunnel.out.log") $log
    Write-Log "Tunnel started (pid $procId, no window); waiting for its public URL"
    $url = ""
    for ($i = 0; $i -lt 30 -and -not $url; $i++) {
        Start-Sleep -Seconds 2
        $url = Find-TunnelUrl @($log)
    }
    if (-not $url) { Write-Log "Tunnel URL not found in tunnel.log yet" "WARN"; return }
    Set-Content -Path $UrlFile -Value $url -Encoding ASCII
    Write-Log "Tunnel URL: $url"
    $frontend = Get-FrontendApiUrl
    if ($frontend -and $frontend -ne $url) {
        Write-Log ("Tunnel URL CHANGED (was $old). The frontend was built for $frontend; " +
                   "sync-frontend.ps1 (see frontend-sync.log) updates it once the new tunnel is stable.") "WARN"
    }
}

# Adopt a tunnel that was already running before the supervisor (its URL came
# from its own log); remember the URL only if it really answers.
function Initialize-TunnelUrl {
    if (Get-TunnelUrl) { return }
    $frontend = Get-FrontendApiUrl
    $candidates = @()
    $found = Find-TunnelUrl @((Join-Path $LogDir "tunnel.log"), (Join-Path $env:TEMP "tunnel.log"), (Join-Path $env:TEMP "tunnel.url"))
    if ($found) { $candidates += $found }
    if ($frontend) { $candidates += $frontend }
    foreach ($c in $candidates) {
        if (Test-Http "$c/api/v1/health" 15) {
            Set-Content -Path $UrlFile -Value $c -Encoding ASCII
            Write-Log "Adopted the running tunnel: $c"
            return
        }
    }
}

# Frontend URL sync runs as its own hidden process (a rebuild + deploy takes minutes);
# sync-frontend.ps1 holds the guards and exits at once when nothing changed.
function Start-FrontendSync {
    if (@(Get-Procs "powershell.exe" @("sync-frontend\.ps1")).Count -gt 0) { return }
    Start-Hidden "powershell.exe" @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
        "-File", $SyncPs1, "-Mode", "sync", "-Auto") | Out-Null
}

# --- the supervisor ----------------------------------------------------------------------------

function Invoke-Supervisor {
    # One supervisor per PC, whichever session started it.
    $mutex = New-Object System.Threading.Mutex($false, "Global\SanaGtmSupervisor")
    $owned = $false
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned -or @(Get-ManagerProcs).Count -gt 0) {
        Write-Log "Another supervisor is already running; exiting."
        if ($owned) { $mutex.ReleaseMutex() }
        return
    }
    try {
        Remove-Item -Force $StopFlag -ErrorAction SilentlyContinue
        Write-Log "Supervisor started (pid $PID, repo $Root)"

        # 1. Database: the API and worker use the staging Supabase PostgreSQL. It is
        #    hosted, so it cannot be started from here -- wait until it answers.
        for ($i = 0; $i -lt 60; $i++) {
            $h = Get-Health
            if ($h -and $h.database) { Write-Log "PostgreSQL (Supabase) reachable"; break }
            if ($i -eq 0) { Write-Log "Waiting for PostgreSQL (Supabase) / network..." "WARN" }
            if (Test-Path $StopFlag) { return }
            Start-Sleep -Seconds 5
        }

        $state = @{
            api    = @{ fails = 0; restarts = 0; next = [datetime]::MinValue; started = [datetime]::MinValue }
            worker = @{ fails = 0; restarts = 0; next = [datetime]::MinValue; started = [datetime]::MinValue }
            tunnel = @{ fails = 0; restarts = 0; next = [datetime]::MinValue; started = [datetime]::MinValue }
        }
        $lastDeep = [datetime]::MinValue
        $lastSync = [datetime]::MinValue
        $firstPass = $true

        while (-not (Test-Path $StopFlag)) {
          $now = Get-Date
          # One bad pass (WMI not ready right after boot, a file lock, ...) must not end
          # the supervisor: log it and try again on the next pass.
          try {

            # 2. API -- one instance; restart if it dies or stops answering.
            $api = @(Get-ApiProcs)
            if ($api.Count -gt 1) {
                Write-Log "Duplicate API processes ($($api.Count)); keeping the oldest" "WARN"
                $api | Sort-Object CreationDate | Select-Object -Skip 1 | ForEach-Object { Stop-Tree $_ }
                $api = @(Get-ApiProcs)
            }
            $s = $state.api
            if ($api.Count -eq 0) {
                if ($now -ge $s.next) {
                    if (-not $firstPass) { Write-Log "API is not running; restarting" "WARN" }
                    Start-Api
                    $s.started = $now; $s.restarts++
                    $s.next = $now.AddSeconds([Math]::Min(300, 10 * [Math]::Pow(2, [Math]::Min(5, $s.restarts - 1))))
                    for ($i = 0; $i -lt 30 -and -not (Test-Http $ApiHealth 5); $i++) { Start-Sleep -Seconds 2 }
                    if (Test-Http $ApiHealth 5) { Write-Log "API healthy: $ApiHealth" } else { Write-Log "API not healthy yet" "WARN" }
                }
            } elseif (($now - $s.started).TotalSeconds -gt 60) {
                if (Test-Http $ApiHealth 10) { $s.fails = 0; if (($now - $s.started).TotalMinutes -gt 10) { $s.restarts = 0 } }
                else {
                    $s.fails++
                    if ($s.fails -ge 3) {
                        Write-Log "API not answering $ApiHealth ($($s.fails) checks); restarting" "WARN"
                        $api | ForEach-Object { Stop-Tree $_ }
                        $s.fails = 0; $s.next = $now
                    }
                }
            }

            # 3. Worker -- only once the API answers.
            $worker = @(Get-WorkerProcs)
            if ($worker.Count -gt 1) {
                Write-Log "Duplicate worker processes ($($worker.Count)); keeping the oldest" "WARN"
                $worker | Sort-Object CreationDate | Select-Object -Skip 1 | ForEach-Object { Stop-Tree $_ }
                $worker = @(Get-WorkerProcs)
            }
            $s = $state.worker
            if ($worker.Count -eq 0 -and $now -ge $s.next -and (Test-Http $ApiHealth 5)) {
                if (-not $firstPass) { Write-Log "Worker is not running; restarting" "WARN" }
                Start-Worker
                $s.started = $now; $s.restarts++
                $s.next = $now.AddSeconds([Math]::Min(300, 10 * [Math]::Pow(2, [Math]::Min(5, $s.restarts - 1))))
            }

            # 4. Tunnel -- adopt the running one; start one only if none is running.
            $tunnel = @(Get-TunnelProcs)
            if ($tunnel.Count -gt 1) {
                Write-Log "Duplicate tunnel processes ($($tunnel.Count)); keeping the oldest" "WARN"
                $tunnel | Sort-Object CreationDate | Select-Object -Skip 1 | ForEach-Object { Stop-Tree $_ }
                $tunnel = @(Get-TunnelProcs)
            }
            $s = $state.tunnel
            if ($tunnel.Count -eq 0) {
                if ($now -ge $s.next -and (Test-Http $ApiHealth 5)) {
                    Write-Log "Tunnel is not running; starting it (a quick tunnel gets a new URL)" "WARN"
                    Start-Tunnel
                    $s.started = $now; $s.restarts++
                    $s.next = $now.AddSeconds([Math]::Min(300, 10 * [Math]::Pow(2, [Math]::Min(5, $s.restarts - 1))))
                }
            } elseif ($firstPass) {
                Initialize-TunnelUrl
            }

            # 5. Deep checks every minute: database, worker heartbeat, public tunnel.
            if (($now - $lastDeep).TotalSeconds -ge 60) {
                $lastDeep = $now
                $h = Get-Health
                if (-not $h -or -not $h.database) { Write-Log "PostgreSQL (Supabase) not reachable" "WARN" }
                $ws = $state.worker
                if ($h -and $h.queue -and -not $h.worker_heartbeat -and $worker.Count -gt 0 -and ($now - $ws.started).TotalSeconds -gt 180) {
                    $ws.fails++
                    if ($ws.fails -ge 3) {
                        Write-Log "Worker process alive but no heartbeat for minutes; restarting" "WARN"
                        $worker | ForEach-Object { Stop-Tree $_ }
                        $ws.fails = 0
                    }
                } else { $ws.fails = 0 }
                if ($tunnel.Count -gt 0) {
                    $logged = Find-TunnelUrl @((Join-Path $LogDir "tunnel.log"))
                    if ($logged -and $logged -ne (Get-TunnelUrl) -and (Test-Http "$logged/api/v1/health" 15)) {
                        Set-Content -Path $UrlFile -Value $logged -Encoding ASCII
                        Write-Log "Tunnel URL (from tunnel.log): $logged"
                    }
                }
                $url = Get-TunnelUrl
                $ts = $state.tunnel
                if ($url -and $tunnel.Count -gt 0) {
                    if (Test-Http "$url/api/v1/health" 15) { $ts.fails = 0 }
                    else {
                        $ts.fails++
                        Write-Log "Tunnel $url not answering (check $($ts.fails))" "WARN"
                        if ($ts.fails -ge 10) {
                            Write-Log "Tunnel dead for 10 minutes; restarting it (URL will change)" "WARN"
                            $tunnel | ForEach-Object { Stop-Tree $_ }
                            $ts.fails = 0
                        }
                    }
                }
            }

            # 6. Frontend URL sync (part B, separate from keeping the services up).
            if ((Get-TunnelUrl) -and ($now - $lastSync).TotalMinutes -ge 5 -and ($now - $state.tunnel.started).TotalSeconds -ge 60) {
                $lastSync = $now
                Start-FrontendSync
            }

            # Services that stayed up for 10 minutes get their restart back-off reset.
            foreach ($k in @("worker", "tunnel")) {
                if (($now - $state[$k].started).TotalMinutes -gt 10) { $state[$k].restarts = 0 }
            }
            $firstPass = $false
          } catch {
            Write-Log ("Supervisor pass failed: {0} (line {1}); continuing" -f $_.Exception.Message, $_.InvocationInfo.ScriptLineNumber) "ERROR"
          }
          for ($i = 0; $i -lt 6 -and -not (Test-Path $StopFlag); $i++) { Start-Sleep -Seconds 5 }
        }
        Write-Log "Stop requested; supervisor exiting"
    } catch {
        Write-Log ("Supervisor crashed: {0} (line {1})" -f $_.Exception.Message, $_.InvocationInfo.ScriptLineNumber) "ERROR"
        throw
    } finally {
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
}

# --- commands ------------------------------------------------------------------------------------

function Show-Status {
    $h = Get-Health
    $api = @(Get-ApiProcs); $worker = @(Get-WorkerProcs); $tunnel = @(Get-TunnelProcs); $mgr = @(Get-ManagerProcs)
    $url = Get-TunnelUrl
    $frontend = Get-FrontendApiUrl
    $pg = [bool]($h -and $h.database)
    $apiOk = ($api.Count -gt 0) -and (Test-Http $ApiHealth 10)
    $workerOk = ($worker.Count -gt 0) -and [bool]($h -and $h.worker_heartbeat)
    $tunnelOk = ($tunnel.Count -gt 0) -and $url -and (Test-Http "$url/api/v1/health" 15)
    $match = $url -and ($url -eq $frontend)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $on = { param($b) if ($b) { "ONLINE" } else { "OFFLINE" } }

    Write-Host ""
    Write-Host ("PostgreSQL: {0}   (staging Supabase database the API/worker use)" -f (& $on $pg))
    Write-Host ("API:        {0}   ({1}{2})" -f (& $on $apiOk), $ApiHealth, $(if ($api.Count) { ", pid $($api[0].ProcessId)" } else { "" }))
    $hb = if ($h -and $h.last_heartbeat_age_s -ne $null) { ", last heartbeat $($h.last_heartbeat_age_s)s ago" } else { "" }
    Write-Host ("Worker:     {0}   ({1} process{2}{3})" -f (& $on $workerOk), $worker.Count, $(if ($worker.Count -eq 1) { "" } else { "es" }), $hb)
    Write-Host ("Tunnel:     {0}   ({1})" -f (& $on $tunnelOk), $(if ($url) { $url } else { "URL unknown" }))
    if ($url -and $frontend -and -not $match) {
        Write-Host ("            WARNING: the live frontend calls {0} -- rebuild + redeploy needed" -f $frontend)
    }
    $ready = $pg -and $apiOk -and $workerOk -and $tunnelOk -and $match
    Write-Host ("Overall:    {0}" -f $(if ($ready) { "READY" } else { "NOT READY" }))
    Write-Host ""
    Write-Host ("Supervisor: {0}    Auto start task '{1}': {2}" -f $(if ($mgr.Count) { "running (pid $($mgr[0].ProcessId))" } else { "not running" }),
        $TaskName, $(if ($task) { "installed ($($task.State))" } else { "not installed" }))
    Write-Host ("Frontend API URL: {0}  ({1})" -f $(if ($frontend) { $frontend } else { "?" }), $(if ($match) { "matches the tunnel" } else { "DOES NOT match the tunnel" }))
    Write-Host ("Logs: {0}" -f $LogDir)
    if ($ready) { return 0 } else { return 1 }
}

function Start-Supervisor {
    if (@(Get-ManagerProcs).Count -gt 0) { Write-Log "Supervisor already running." }
    else {
        Remove-Item -Force $StopFlag -ErrorAction SilentlyContinue
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Start-ScheduledTask -TaskName $TaskName
            Write-Log "Started the scheduled task '$TaskName'."
        } else {
            Start-Hidden "powershell.exe" (Get-SupervisorArgs) | Out-Null
            Write-Log "Started the supervisor (task not installed)."
        }
    }
    Write-Host "Waiting for the services (up to 2 minutes)..."
    for ($i = 0; $i -lt 24; $i++) {
        Start-Sleep -Seconds 5
        if (@(Get-ApiProcs).Count -and (Test-Http $ApiHealth 5) -and @(Get-WorkerProcs).Count -and @(Get-TunnelProcs).Count -and (Get-TunnelUrl)) { break }
    }
    Show-Status | Out-Null
}

function Stop-All {
    Set-Content -Path $StopFlag -Value (Get-Date -Format o) -Encoding ASCII
    for ($i = 0; $i -lt 12 -and @(Get-ManagerProcs).Count -gt 0; $i++) { Start-Sleep -Seconds 1 }
    Get-ManagerProcs | ForEach-Object { Stop-Tree $_ }
    Get-WorkerProcs | ForEach-Object { Stop-Tree $_; Write-Log "Worker stopped (pid $($_.ProcessId))" }
    Get-ApiProcs | ForEach-Object { Stop-Tree $_; Write-Log "API stopped (pid $($_.ProcessId))" }
    Get-TunnelProcs | ForEach-Object { Stop-Tree $_; Write-Log "Tunnel stopped (pid $($_.ProcessId)); the next start gets a new URL" }
    Remove-Item -Force $UrlFile -ErrorAction SilentlyContinue
    Write-Log "SANA GTM stopped."
}

function Get-SupervisorArgs {
    @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", $PSCommandPath, "-Action", "run")
}

function Install-Task {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    # Task Scheduler cannot start a console program without a console window, so the task
    # runs pythonw.exe (a GUI program: no console at all), which starts
    #   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File sana-gtm.ps1 -Action run
    # with CREATE_NO_WINDOW and waits for it (so the task shows Running while the supervisor runs).
    $psArgs = (Get-SupervisorArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join " "
    $action = New-ScheduledTaskAction -Execute $Pythonw -WorkingDirectory $Root -Argument `
        "`"$Launcher`" --cwd `"$Root`" --stdout `"$(Join-Path $LogDir 'supervisor.out.log')`" --stderr `"$(Join-Path $LogDir 'supervisor.err.log')`" --wait -- powershell.exe $psArgs"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = "PT30S"   # let the network come up after logon
    # A second trigger re-fires every minute (while this user is logged on): if the
    # supervisor ever exits it is back within a minute. While the supervisor runs the
    # repeat does nothing (IgnoreNew: no process is even started).
    $watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($trigger, $watchdog) -Settings $settings -Principal $principal `
        -Description "Starts and supervises the SANA GTM API, worker and Cloudflare quick tunnel on this PC ($Root)." -Force | Out-Null
    Write-Log "Installed scheduled task '$TaskName' (at logon of $user, 30 s delay, watchdog every minute, no console window)."
}

function Uninstall-Task {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Log "Removed scheduled task '$TaskName'. Running services were left as they are (use stop-sana-gtm.bat)."
    } else { Write-Log "Scheduled task '$TaskName' is not installed." }
}

switch ($Action) {
    "run"       { Invoke-Supervisor }
    "start"     { Start-Supervisor }
    "stop"      { Stop-All }
    "status"    { exit (Show-Status) }
    "install"   { Install-Task }
    "uninstall" { Uninstall-Task }
}
