<#
.SYNOPSIS
  Read-only readiness check of this Windows PC for the SANA GTM VM (VMware Workstation +
  Ubuntu Server 24.04). Needs no administrator rights and changes nothing.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\selfhost\windows\check-host.ps1 [-VmDrive E] [-Json out.json]
#>
param(
    [string]$VmDrive = "E",
    [string]$Json = ""
)
$ErrorActionPreference = "SilentlyContinue"
$results = New-Object System.Collections.Generic.List[object]
function Add-Check($name, $status, $detail) {
    $results.Add([pscustomobject]@{ check = $name; status = $status; detail = $detail })
}

# --- operating system and rights ---------------------------------------------------------------
$os = Get-CimInstance Win32_OperatingSystem
Add-Check "Windows" "info" ("{0} build {1}" -f $os.Caption, $os.BuildNumber)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
$inAdminGroup = (whoami /groups) -match "S-1-5-32-544"
if ($isAdmin) { Add-Check "Administrator rights" "ok" "this session is elevated" }
elseif ($inAdminGroup) { Add-Check "Administrator rights" "warn" "member of Administrators but not elevated: run the VMware installer with 'Run as administrator'" }
else { Add-Check "Administrator rights" "BLOCKER" "standard user: an administrator must install VMware Workstation once (see INSTALL.md, step 1)" }

# --- CPU / virtualization ------------------------------------------------------------------------
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
Add-Check "CPU" "info" ("{0}, {1} cores / {2} threads" -f $cpu.Name.Trim(), $cpu.NumberOfCores, $cpu.NumberOfLogicalProcessors)
if ($cpu.NumberOfLogicalProcessors -ge 6) { Add-Check "CPU for a 4-vCPU VM" "ok" "leaves $($cpu.NumberOfLogicalProcessors - 4) threads for Windows" }
else { Add-Check "CPU for a 4-vCPU VM" "warn" "use 2 vCPU" }
$cs = Get-CimInstance Win32_ComputerSystem
if ($cs.HypervisorPresent) {
    Add-Check "Virtualization" "ok" "a hypervisor is running (VBS/Hyper-V platform): VT-x is enabled in firmware. VMware Workstation 17.5+ runs on top of it via Windows Hypervisor Platform (somewhat slower than bare VT-x)"
} elseif ($cpu.VirtualizationFirmwareEnabled) {
    Add-Check "Virtualization" "ok" "VT-x enabled in firmware"
} else {
    Add-Check "Virtualization" "BLOCKER" "VT-x appears disabled: enable Intel Virtualization Technology in the BIOS/UEFI"
}
$dg = Get-CimInstance -Namespace root\Microsoft\Windows\DeviceGuard -ClassName Win32_DeviceGuard
if ($dg) {
    $vbs = @{0 = "off"; 1 = "enabled, not running"; 2 = "running"}[[int]$dg.VirtualizationBasedSecurityStatus]
    Add-Check "Virtualization-based security" "info" "VBS $vbs (fine for VMware 17.5+)"
}
$whp = Get-CimInstance Win32_OptionalFeature -Filter "Name='HypervisorPlatform'"
if ($whp) { Add-Check "Windows Hypervisor Platform feature" "info" ("state {0} (1 = enabled; VMware enables what it needs during its own install)" -f $whp.InstallState) }

# --- VMware ------------------------------------------------------------------------------------
$uninstall = @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
               "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*")
$vmware = Get-ItemProperty $uninstall | Where-Object { $_.DisplayName -match "VMware Workstation|VMware Player" } | Select-Object -First 1
$vmrun = @("C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe", "C:\Program Files\VMware\VMware Workstation\vmrun.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if ($vmware -or $vmrun) { Add-Check "VMware Workstation" "ok" ("{0} {1}" -f $vmware.DisplayName, $vmware.DisplayVersion) }
else { Add-Check "VMware Workstation" "BLOCKER" "not installed (administrator needed): download VMware Workstation Pro from Broadcom (free)" }

# --- memory ------------------------------------------------------------------------------------
$totalGb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
$freeGb = [math]::Round($os.FreePhysicalMemory * 1KB / 1GB, 1)
if ($totalGb -ge 16) { Add-Check "RAM" "ok" "$totalGb GB total, $freeGb GB free now; the VM takes 8 GB" }
else { Add-Check "RAM" "warn" "$totalGb GB total: use a 4 GB VM (minimum profile)" }

# --- disks -------------------------------------------------------------------------------------
$vol = Get-Volume -DriveLetter $VmDrive
if ($vol) {
    $free = [math]::Round($vol.SizeRemaining / 1GB)
    $status = if ($free -ge 130) { "ok" } elseif ($free -ge 60) { "warn" } else { "BLOCKER" }
    Add-Check "VM drive ${VmDrive}:" $status "$free GB free (recommended VM: 30 GB OS + 80 GB data disk, thin; keep 20 GB spare)"
} else { Add-Check "VM drive ${VmDrive}:" "BLOCKER" "drive not found" }
$c = Get-Volume -DriveLetter C
if ($c) {
    $cfree = [math]::Round($c.SizeRemaining / 1GB)
    $status = if ($cfree -ge 15) { "ok" } else { "warn" }
    Add-Check "System drive C:" $status "$cfree GB free (VMware itself needs ~2 GB; keep the VM off C:)"
}
$disks = @(Get-CimInstance Win32_DiskDrive)
$diskText = ($disks | ForEach-Object { "{0} ({1} GB)" -f $_.Model, [math]::Round($_.Size / 1GB) }) -join "; "
if ($disks.Count -ge 2) { Add-Check "Physical disks" "ok" $diskText }
else { Add-Check "Physical disks" "warn" "$diskText - only one disk: backups must also go to a USB disk or another machine" }

# --- power: the VM stops when Windows sleeps ---------------------------------------------------
$standby = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE) -match "Current AC Power Setting Index" |
    Select-Object -First 1
if ($standby -match "0x([0-9a-f]+)") {
    $mins = [Convert]::ToInt32($Matches[1], 16) / 60
    if ($mins -eq 0) { Add-Check "Sleep on AC power" "ok" "never" }
    else { Add-Check "Sleep on AC power" "warn" "Windows sleeps after $mins min idle, which pauses the VM: Settings > System > Power > Screen and sleep > 'Never'" }
}

# --- network -----------------------------------------------------------------------------------
$adapters = Get-NetAdapter | Where-Object Status -eq "Up"
Add-Check "Network adapters up" "info" (($adapters | ForEach-Object { "{0} ({1})" -f $_.Name, $_.LinkSpeed }) -join "; ")
foreach ($target in @(
    @{ name = "Docker apt repository"; host = "download.docker.com"; port = 443 },
    @{ name = "GitHub"; host = "github.com"; port = 443 },
    @{ name = "Ubuntu mirrors"; host = "archive.ubuntu.com"; port = 80 },
    @{ name = "Cloudflare Tunnel edge (TCP 7844)"; host = "region1.v2.argotunnel.com"; port = 7844 },
    @{ name = "Supabase Auth"; host = "supabase.co"; port = 443 })) {
    $t = Test-NetConnection -ComputerName $target.host -Port $target.port -WarningAction SilentlyContinue
    Add-Check $target.name ($(if ($t.TcpTestSucceeded) { "ok" } else { "warn" })) ("{0}:{1} {2}" -f $target.host, $target.port,
        $(if ($t.TcpTestSucceeded) { "reachable" } else { "NOT reachable" }))
}

# --- security software that has interfered before ----------------------------------------------
$webroot = Get-Service | Where-Object { $_.Name -match "WRSVC|Webroot" }
if ($webroot) { Add-Check "Webroot" "warn" "installed ($($webroot.Status)); it deleted build output before - allow-list E:\VMs if VM files disappear" }

# --- the current SANA GTM services on Windows (they keep running until cutover) ---------------
$task = Get-ScheduledTask -TaskName "SANA GTM Auto Start"
if ($task) { Add-Check "Current SANA GTM (Windows)" "info" "scheduled task '$($task.TaskName)' is $($task.State); keep it until the VM is verified" }

$results | Format-Table -AutoSize -Wrap
$blockers = @($results | Where-Object status -eq "BLOCKER").Count
"{0} blocker(s), {1} warning(s)" -f $blockers, @($results | Where-Object status -eq "warn").Count
if ($Json) { $results | ConvertTo-Json -Depth 3 | Out-File -Encoding utf8 $Json }
exit ([int]($blockers -gt 0))
