<#
.SYNOPSIS
  Build SANA-GTM-Windows-Installer.zip from this repository (developer machine).

.DESCRIPTION
  1. builds the dashboard once (auth settings are supplied at run time, so nothing is baked in)
  2. stages the installer, management scripts, application source, dashboard and docs. Only files
     git tracks or would track (git ls-files) are copied, so ignored files - .env files, dumps,
     keys, local databases - can never enter the package
  3. refuses to continue if the secret scan finds anything
  4. writes PACKAGE-INFO.json (commit, build time) and SHA256SUMS.txt, then the ZIP

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows-selfhost\Build-Package.ps1 -OutDir E:\Crawlers\sana-win-build\out
#>
param(
    [string]$OutDir = '',
    [string]$Python = '',
    [string[]]$RealSecretEnvFiles = @(),
    [switch]$SkipDashboardBuild
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Here = $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $Repo 'dist-windows' }
if (-not $Python) { $Python = Join-Path $Repo 'cloud\.venv\Scripts\python.exe' }
$Name = 'SANA-GTM-Windows-Installer'
$Stage = Join-Path $OutDir $Name
if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
New-Item -ItemType Directory -Path $Stage -Force | Out-Null

function Copy-Tracked([string[]]$Paths, [string]$DestRoot, [string[]]$ExcludePrefixes = @()) {
    $list = & git -C $Repo ls-files --cached --others --exclude-standard -- @Paths
    if ($LASTEXITCODE -ne 0) { throw 'git ls-files failed' }
    $n = 0
    foreach ($rel in $list) {
        if ($ExcludePrefixes | Where-Object { $rel.StartsWith($_) }) { continue }
        $src = Join-Path $Repo $rel
        if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { continue }
        $dst = Join-Path $DestRoot $rel
        $dir = Split-Path -Parent $dst
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Copy-Item -LiteralPath $src -Destination $dst
        $n++
    }
    return $n
}

Write-Host '== dashboard'
$web = Join-Path $Repo 'cloud\web'
if (-not $SkipDashboardBuild) {
    $env:VITE_AUTH_MODE = 'supabase'; $env:VITE_DEPLOY_ENV = 'production'; $env:VITE_API_URL = ''
    $env:VITE_SUPABASE_URL = ''; $env:VITE_SUPABASE_ANON_KEY = ''
    Push-Location $web
    try {
        & node node_modules/typescript/bin/tsc --noEmit
        if ($LASTEXITCODE -ne 0) { throw 'dashboard typecheck failed' }
        & node node_modules/vite/bin/vite.js build --mode production --outDir dist-selfhost --emptyOutDir
        if ($LASTEXITCODE -ne 0) { throw 'dashboard build failed' }
    } finally { Pop-Location; Remove-Item Env:VITE_* -ErrorAction SilentlyContinue }
}
$built = Join-Path $web 'dist-selfhost'
# Installation-neutral: no API address, no tunnel URL, no project URL (the supabase-js library itself
# mentions ".supabase.co" in its code; a baked-in project would be https://<20-char ref>.supabase.co).
foreach ($bad in @('localhost:8000', '127\.0\.0\.1:8000', 'trycloudflare\.com', 'service_role', 'https://[a-z0-9]{20}\.supabase\.co')) {
    $hit = Get-ChildItem -LiteralPath (Join-Path $built 'assets') -File | Where-Object { (Get-Content -LiteralPath $_.FullName -Raw) -match $bad }
    if ($hit) { throw "dashboard bundle matches '$bad' ($($hit[0].Name)): it must stay installation-neutral" }
}
Copy-Item -LiteralPath $built -Destination (Join-Path $Stage 'web\dist') -Recurse
Write-Host '   dashboard built (no auth origin or key baked in; /runtime-config.js supplies them)'

Write-Host '== installer and management scripts'
foreach ($f in @('Install-SanaGtm.ps1', 'INSTALL.cmd', 'components.json', 'requirements-lock.txt', 'README-WINDOWS.md')) {
    Copy-Item -LiteralPath (Join-Path $Here $f) -Destination $Stage
}
Copy-Item -LiteralPath (Join-Path $Here 'bin') -Destination (Join-Path $Stage 'bin') -Recurse
Copy-Item -LiteralPath (Join-Path $Here 'config') -Destination (Join-Path $Stage 'config') -Recurse
Copy-Item -LiteralPath (Join-Path $Here 'tests') -Destination (Join-Path $Stage 'tests') -Recurse

Write-Host '== application source'
$n = Copy-Tracked @('adapters', 'config', 'crawler', 'exporters', 'models', 'utils', 'main.py', 'cloud', 'deploy/ops') (Join-Path $Stage 'app') `
    @('cloud/web/', 'cloud/demo/')
Write-Host "   $n files (git-tracked only; cloud/web source and Google Sheets/SQLite tooling not included)"
Get-ChildItem -LiteralPath (Join-Path $Stage 'app') -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

Write-Host '== documentation'
$null = Copy-Tracked @('docs/self-hosting-audit', 'deploy/selfhost/README.md') (Join-Path $Stage 'docs')

$commit = (& git -C $Repo rev-parse HEAD).Trim()
$dirty = [bool](& git -C $Repo status --porcelain)
$components = Get-Content -LiteralPath (Join-Path $Here 'components.json') -Raw | ConvertFrom-Json
$info = [ordered]@{
    product = 'SANA GTM (self-hosted, Windows)'; commit = $commit; working_tree_dirty = $dirty
    built_at = (Get-Date).ToUniversalTime().ToString('s') + 'Z'
    python = $components.python.version; postgresql = $components.postgresql.version
    winsw = $components.winsw.version; cloudflared = $components.cloudflared.version
}
[IO.File]::WriteAllText((Join-Path $Stage 'PACKAGE-INFO.json'), ($info | ConvertTo-Json))

Write-Host '== secret scan'
$scanArgs = @((Join-Path $Here 'tools\scan_package.py'), $Stage)
# (powershell -File passes 'a','b' as one string "a,b": split it)
$patterns = @($RealSecretEnvFiles | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
if ($patterns.Count -gt 0) { $scanArgs += '--real-values-from'; $scanArgs += $patterns }
& $Python @scanArgs
if ($LASTEXITCODE -ne 0) { throw 'secret scan failed: the package was NOT built' }

Write-Host '== checksums and zip'
$sums = Get-ChildItem -LiteralPath $Stage -Recurse -File | Sort-Object FullName | ForEach-Object {
    '{0}  {1}' -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.FullName.Substring($Stage.Length + 1).Replace('\', '/')
}
[IO.File]::WriteAllLines((Join-Path $Stage 'SHA256SUMS.txt'), $sums)
$zip = Join-Path $OutDir "$Name.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($Stage, $zip, [IO.Compression.CompressionLevel]::Optimal, $true)
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
Set-Content -LiteralPath "$zip.sha256" -Value "$hash  $Name.zip" -Encoding ascii
Write-Host ("built {0} ({1:N1} MB, {2} files) sha256 {3}" -f $zip, ((Get-Item $zip).Length / 1MB), $sums.Count, $hash)
Write-Host "commit $commit$(if ($dirty) { ' + uncommitted changes' } else { '' })"
