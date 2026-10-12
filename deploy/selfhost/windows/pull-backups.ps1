<#
.SYNOPSIS
  Copy the SANA GTM VM's newest database backups OFF the VM (to a USB disk or another drive) and
  check every copied dump against its manifest SHA-256. Runs as a normal user; uses the Windows
  OpenSSH client (scp/ssh) with a key that may only read /srv/sanagtm/backups.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File pull-backups.ps1 -VmHost 192.168.56.10 -User sanabackup -Destination F:\SanaBackups
  # schedule it daily (no admin needed):
  schtasks /Create /SC DAILY /ST 03:15 /TN "SANA GTM pull backups" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File E:\VMs\sanagtm\pull-backups.ps1 -VmHost 192.168.56.10 -User sanabackup -Destination F:\SanaBackups"
#>
param(
    [Parameter(Mandatory = $true)][string]$VmHost,
    [Parameter(Mandatory = $true)][string]$User,
    [Parameter(Mandatory = $true)][string]$Destination,
    [int]$Keep = 30,
    [string]$Key = "$env:USERPROFILE\.ssh\sanagtm_backup"
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path $Destination)) { throw "destination $Destination is not available (is the USB disk connected?)" }
$destDrive = (Get-Item $Destination).PSDrive.Name
$vmDisk = (Get-Partition -DriveLetter E -ErrorAction SilentlyContinue).DiskNumber
$destDisk = (Get-Partition -DriveLetter $destDrive -ErrorAction SilentlyContinue).DiskNumber
if ($null -ne $vmDisk -and $vmDisk -eq $destDisk) {
    Write-Warning "$Destination is on the same physical disk as the VM (disk $vmDisk): this protects against mistakes, not disk failure"
}
$remote = "${User}@${VmHost}"
$names = & ssh -i $Key -o BatchMode=yes $remote "ls -1t /srv/sanagtm/backups/*.dump | head -n 3"
if ($LASTEXITCODE -ne 0) { throw "cannot list backups on $VmHost" }
$copied = 0
foreach ($dump in $names) {
    $base = [IO.Path]::GetFileNameWithoutExtension($dump)
    $localDump = Join-Path $Destination "$base.dump"
    if (Test-Path $localDump) { continue }
    & scp -i $Key -o BatchMode=yes "${remote}:$dump" "${remote}:/srv/sanagtm/backups/$base.manifest.json" $Destination
    if ($LASTEXITCODE -ne 0) { throw "copy of $base failed" }
    $manifest = Get-Content (Join-Path $Destination "$base.manifest.json") -Raw | ConvertFrom-Json
    $hash = (Get-FileHash $localDump -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $manifest.sha256) { Remove-Item $localDump; throw "$base.dump does not match its manifest SHA-256" }
    $copied++
    Write-Output ("copied {0} ({1:N1} MB, {2:N0} rows), SHA-256 verified" -f $base, ($manifest.bytes / 1MB), $manifest.rows_total)
}
Get-ChildItem $Destination -Filter *.dump | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep | ForEach-Object {
    Remove-Item $_.FullName, ($_.FullName -replace '\.dump$', '.manifest.json') -ErrorAction SilentlyContinue
}
Write-Output "$copied new backup(s) copied to $Destination"
