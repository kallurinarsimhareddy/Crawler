<#
  SANA GTM end-to-end verification (read-only unless -KillTests).

    verify-sana-gtm.ps1                 check processes, health, tunnel, live site, frontend URL
    verify-sana-gtm.ps1 -KillTests      also kill the API, the worker and the supervisor (one at a
                                        time) and time how long until each is back
    verify-sana-gtm.ps1 -WaitForSync N  wait up to N minutes for the frontend URL auto-sync

  Writes logs\sana-gtm\verify-<time>.txt (and prints it). Run by the one-shot task
  "SANA GTM Reboot Verify" after a reboot test; with -RemoveTask it unregisters that
  task when done. Never prints secrets: healthcheck.py reports booleans only.
#>
param(
    [switch]$KillTests,
    [int]$WaitForSync = 0,
    [switch]$RemoveTask
)

$ErrorActionPreference = "Continue"
$Root     = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$LogDir   = Join-Path $Root "logs\sana-gtm"
$Python   = Join-Path $Root "cloud\.venv\Scripts\python.exe"
$Report   = Join-Path $LogDir ("verify-{0}.txt" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$ApiHealth = "http://127.0.0.1:8100/api/v1/health"
$script:Failures = 0

function Out-Line([string]$Text) { Add-Content -Path $Report -Value $Text -Encoding UTF8; Write-Host $Text }
function Check([string]$Name, [bool]$Ok, [string]$Detail = "") {
    if (-not $Ok) { $script:Failures++ }
    Out-Line ("[{0}] {1,-34} {2}" -f $(if ($Ok) { "PASS" } else { "FAIL" }), $Name, $Detail)
}
function Test-Http([string]$Url, [int]$TimeoutSec = 15) {
    try { return ((Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec).StatusCode -eq 200) } catch { return $false }
}
function Get-Roots([string]$Name, [string]$Pattern) {
    $p = @(Get-CimInstance Win32_Process -Filter "Name='$Name'" | Where-Object { $_.CommandLine -match $Pattern })
    $ids = @($p | ForEach-Object { $_.ProcessId })
    @($p | Where-Object { $ids -notcontains $_.ParentProcessId })
}
function Get-Api        { Get-Roots "python.exe" 'cloud\.api\.main:app' }
function Get-Worker     { Get-Roots "python.exe" 'cloud\.intel\.tasks\.worker' }
function Get-Tunnel     { Get-Roots "cloudflared.exe" 'tunnel.*--url\s+http://127\.0\.0\.1:8100' }
function Get-Supervisor { Get-Roots "powershell.exe" 'sana-gtm\.ps1.*-Action\s+run' }
function Get-ParentName($proc) {
    try { (Get-Process -Id $proc.ParentProcessId -ErrorAction Stop).ProcessName } catch { "(exited)" }
}
function Get-Health {
    try { (& $Python (Join-Path $PSScriptRoot "healthcheck.py") (Join-Path $Root "cloud\worker\.env.sana-cloud") 2>$null |
        Select-Object -Last 1 | ConvertFrom-Json) } catch { $null }
}
function Get-LiveFrontendApiUrl {
    try {
        $r = Invoke-WebRequest -Uri "https://sanagtm.pages.dev/?v=$(Get-Random)" -UseBasicParsing -TimeoutSec 20
        $m = [regex]::Match([string]$r.Headers["Content-Security-Policy"], 'connect-src[^;]*?(https://[a-z0-9-]+\.trycloudflare\.com)')
        if ($m.Success) { return $m.Groups[1].Value }
    } catch { }
    ""
}
# No SANA GTM process may have a console window: everything runs with CREATE_NO_WINDOW
# (launch_hidden.py). console_window.py attaches to each process's console and reports
# whether it has a window (a conhost window, or a Windows Terminal tab via OpenConsole).
function Get-SanaTree {
    $all = @(Get-CimInstance Win32_Process)
    $ids = @(@(Get-Supervisor) + @(Get-Api) + @(Get-Worker) + @(Get-Tunnel) | ForEach-Object { $_.ProcessId })
    $ids += @($all | Where-Object { $_.Name -eq "pythonw.exe" -and $_.CommandLine -match 'launch_hidden\.py' } | ForEach-Object { $_.ProcessId })
    $queue = New-Object System.Collections.Queue; $ids | ForEach-Object { $queue.Enqueue($_) }
    $seen = @{}
    while ($queue.Count) {
        $id = $queue.Dequeue(); if ($seen[$id]) { continue }; $seen[$id] = $true
        $all | Where-Object { $_.ParentProcessId -eq $id -and $_.Name -ne "conhost.exe" } | ForEach-Object { $queue.Enqueue($_.ProcessId) }
    }
    @($all | Where-Object { $seen[$_.ProcessId] })
}
function Get-SanaConsoleWindows {
    $tree = @(Get-SanaTree)
    if (-not $tree) { return @() }
    $names = @{}; $tree | ForEach-Object { $names[[int]$_.ProcessId] = $_.Name }
    @(& $Python (Join-Path $PSScriptRoot "console_window.py") @($tree | ForEach-Object { $_.ProcessId }) 2>$null |
        ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.window } |
        ForEach-Object { "{0} (pid {1}) has a console window via {2}{3}" -f $names[[int]$_.pid], $_.pid, $_.host, $(if ($_.visible) { ", VISIBLE" } else { "" }) })
}
# While waiting for restarts: any new Windows Terminal tab (OpenConsole) or a visible
# classic window of a console program is recorded (sampled every 0.5 s).
$script:SeenWindows = @{}
$script:Started = Get-Date
function Get-VisibleConsoleWindows {
    @(Get-Process -Name OpenConsole -ErrorAction SilentlyContinue | Where-Object { $_.StartTime -gt $script:Started } |
        ForEach-Object { "new Windows Terminal tab (OpenConsole pid {0}, {1:HH:mm:ss})" -f $_.Id, $_.StartTime }) +
    @(Get-Process -Name conhost, powershell, pwsh, cmd, python, pythonw, cloudflared, node -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } |
        ForEach-Object { "{0} (pid {1}) window '{2}'" -f $_.ProcessName, $_.Id, $_.MainWindowTitle })
}
function Watch-Windows { foreach ($w in Get-VisibleConsoleWindows) { $script:SeenWindows[$w] = $true } }
function Wait-For([scriptblock]$Condition, [int]$Seconds) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
        if (& $Condition) { Watch-Windows; return [int]$sw.Elapsed.TotalSeconds }
        for ($i = 0; $i -lt 6; $i++) { Watch-Windows; Start-Sleep -Milliseconds 500 }
    }
    return -1
}

# --- state ---------------------------------------------------------------------------------------
$os = Get-CimInstance Win32_OperatingSystem
$explorer = Get-Process explorer -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -First 1
Out-Line "SANA GTM verification  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Out-Line "Last boot:   $($os.LastBootUpTime)"
Out-Line "User logon:  $(if ($explorer) { $explorer.StartTime } else { '?' })   (explorer start)"
$task = Get-ScheduledTask -TaskName "SANA GTM Auto Start" -ErrorAction SilentlyContinue
Out-Line "Task:        $(if ($task) { "installed, $($task.State)" } else { 'NOT INSTALLED' })"
Out-Line ""

$sup = @(Get-Supervisor); $api = @(Get-Api); $wk = @(Get-Worker); $tn = @(Get-Tunnel)
Check "Supervisor running (exactly 1)" ($sup.Count -eq 1) ("pid {0}, parent {1}, started {2}" -f ($sup.ProcessId -join ","), $(if ($sup) { Get-ParentName $sup[0] }), $(if ($sup) { $sup[0].CreationDate }))
Check "Supervisor launched by the task" ($sup.Count -ge 1 -and (Get-ParentName $sup[0]) -eq "pythonw") "parent must be pythonw (launch_hidden.py run by Task Scheduler), not a terminal"
Check "API process (exactly 1)"       ($api.Count -eq 1) ("pid {0}, started {1}" -f ($api.ProcessId -join ","), $(if ($api) { $api[0].CreationDate }))
Check "Worker process (exactly 1)"    ($wk.Count -eq 1)  ("pid {0}, started {1}" -f ($wk.ProcessId -join ","), $(if ($wk) { $wk[0].CreationDate }))
Check "Tunnel process (exactly 1)"    ($tn.Count -eq 1)  ("pid {0}, started {1}" -f ($tn.ProcessId -join ","), $(if ($tn) { $tn[0].CreationDate }))
foreach ($p in @($api + $wk + $tn)) {
    # Started services' parent is launch_hidden.py, which exits at once ("(exited)").
    if ($p -and $sup.Count -ge 1 -and (Get-ParentName $p) -ne "(exited)") {
        Out-Line "       note: pid $($p.ProcessId) parent is $(Get-ParentName $p) (adopted, not started by this supervisor)"
    }
}
$win = @(Get-SanaConsoleWindows)
Check "No console windows (SANA procs)" ($win.Count -eq 0) $(if ($win.Count) { $win -join "; " } else { "$(@(Get-SanaTree).Count) processes checked, none has a console window" })
$win = @(Get-VisibleConsoleWindows | Where-Object { $_ -notmatch '^new Windows Terminal tab' })
Check "No visible console windows"    ($win.Count -eq 0) $(if ($win.Count) { $win -join "; " } else { "none (CMD / PowerShell / Python / cloudflared / conhost)" })
Check "API local health"              (Test-Http $ApiHealth) $ApiHealth
$h = Get-Health
Check "PostgreSQL (Supabase)"         ([bool]($h -and $h.database)) ""
Check "Queue (Upstash)"               ([bool]($h -and $h.queue)) ""
Check "Worker heartbeat"              ([bool]($h -and $h.worker_heartbeat)) ("last heartbeat {0}s ago, {1} online" -f $h.last_heartbeat_age_s, $h.workers_online)
$url = (Get-Content (Join-Path $LogDir "tunnel-url.txt") -ErrorAction SilentlyContinue | Select-Object -First 1)
Check "Tunnel public API health"      ([bool]($url -and (Test-Http "$url/api/v1/health"))) "$url/api/v1/health"
Check "https://sanagtm.pages.dev"     (Test-Http "https://sanagtm.pages.dev/") "HTTP 200"
$live = Get-LiveFrontendApiUrl
if ($live -ne $url -and $WaitForSync -gt 0) {
    Out-Line "       frontend calls $live, tunnel is $url -- waiting up to $WaitForSync min for the auto-sync"
    $t = Wait-For { (Get-LiveFrontendApiUrl) -eq $url } ($WaitForSync * 60)
    $live = Get-LiveFrontendApiUrl
    Out-Line "       auto-sync: $(if ($t -ge 0) { "done after ${t}s of waiting" } else { 'did not happen in time (see frontend-sync.log)' })"
}
Check "Live frontend calls the tunnel" ($live -and $live -eq $url) "frontend=$live"
Check "Live frontend -> public API"   ([bool]($live -and (Test-Http "$live/api/v1/health"))) "$live/api/v1/health"

# --- kill tests ----------------------------------------------------------------------------------
if ($KillTests) {
    Out-Line ""
    $old = @(Get-Api)
    if ($old) {
        $old | ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F 2>&1 | Out-Null }
        $t = Wait-For { $n = @(Get-Api); $n.Count -eq 1 -and $n[0].ProcessId -ne $old[0].ProcessId -and (Test-Http $ApiHealth 5) } 180
        Check "Kill API -> recovered"      ($t -ge 0) $(if ($t -ge 0) { "healthy again after ${t}s (new pid $((Get-Api).ProcessId))" } else { "not back within 180s" })
    } else { Check "Kill API -> recovered" $false "no API process to kill" }

    $old = @(Get-Worker)
    if ($old) {
        $old | ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F 2>&1 | Out-Null }
        $t = Wait-For { $n = @(Get-Worker); $n.Count -eq 1 -and $n[0].ProcessId -ne $old[0].ProcessId } 180
        Check "Kill worker -> recovered"   ($t -ge 0) $(if ($t -ge 0) { "running again after ${t}s (new pid $((Get-Worker).ProcessId))" } else { "not back within 180s" })
        # Fresh = sent after the new worker started (not the killed worker's last one).
        $t2 = Wait-For { $x = Get-Health; $w = @(Get-Worker)
            [bool]($x -and $w -and $x.worker_heartbeat -and $x.last_heartbeat_age_s -ne $null -and
                   $x.last_heartbeat_age_s -lt ((Get-Date) - $w[0].CreationDate).TotalSeconds) } 120
        Check "Worker heartbeat after restart" ($t2 -ge 0) $(if ($t2 -ge 0) { "fresh heartbeat after ${t2}s more" } else { "no fresh heartbeat within 120s" })
    } else { Check "Kill worker -> recovered" $false "no worker process to kill" }

    $old = @(Get-Supervisor)
    $before = @((Get-Api).ProcessId, (Get-Worker).ProcessId, (Get-Tunnel).ProcessId) -join ","
    if ($old) {
        $old | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }   # the supervisor only; services keep running
        $t = Wait-For { $n = @(Get-Supervisor); $n.Count -eq 1 -and $n[0].ProcessId -ne $old[0].ProcessId } 180
        Check "Kill supervisor -> watchdog"  ($t -ge 0) $(if ($t -ge 0) { "new supervisor after ${t}s (pid $((Get-Supervisor).ProcessId), parent $(Get-ParentName (Get-Supervisor)[0]))" } else { "not back within 180s" })
        Start-Sleep -Seconds 20
        $after = @((Get-Api).ProcessId, (Get-Worker).ProcessId, (Get-Tunnel).ProcessId) -join ","
        Check "Services adopted, not duplicated" ($before -eq $after -and @(Get-Api).Count -eq 1 -and @(Get-Worker).Count -eq 1 -and @(Get-Tunnel).Count -eq 1) "api,worker,tunnel pids before $before / after $after"
    } else { Check "Kill supervisor -> watchdog" $false "no supervisor to kill" }
    Check "Tunnel URL unchanged by kill tests" ((Get-Content (Join-Path $LogDir "tunnel-url.txt") | Select-Object -First 1) -eq $url) $url
    Check "Public API after kill tests" (Test-Http "$url/api/v1/health") ""
    Watch-Windows
    $win = @(Get-SanaConsoleWindows)
    Check "No console windows after restarts" ($win.Count -eq 0) $(if ($win.Count) { $win -join "; " } else { "restarted API/worker/supervisor have no console window" })
    Check "No window during restarts"   ($script:SeenWindows.Count -eq 0) $(if ($script:SeenWindows.Count) { $script:SeenWindows.Keys -join "; " } else { "none seen (sampled every 0.5 s)" })
}

Out-Line ""
Out-Line ("RESULT: {0}  ({1} failed checks)" -f $(if ($script:Failures -eq 0) { "ALL PASS" } else { "FAILURES" }), $script:Failures)
Out-Line "Report: $Report"
if ($RemoveTask) { Unregister-ScheduledTask -TaskName "SANA GTM Reboot Verify" -Confirm:$false -ErrorAction SilentlyContinue }
exit $script:Failures
