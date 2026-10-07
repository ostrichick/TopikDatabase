param(
    [string]$Runtime = (Join-Path (Split-Path $PSScriptRoot -Parent) '..\TopikDatabase-runtime'),
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$runtimePath = (Resolve-Path -LiteralPath $Runtime).Path
$config = Get-Content -LiteralPath (Join-Path $runtimePath 'operational.json') -Raw | ConvertFrom-Json
if (-not $config.database_url.StartsWith('postgresql://')) { throw 'PostgreSQL configuration is required' }
if ($config.database_url -notmatch '/topik\?') { throw 'The operational topik database is required' }
if ($config.database_url -notmatch 'sslmode=verify-full') { throw 'TLS identity verification is required' }
$listener = Get-NetTCPConnection -LocalPort 55432 -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    $tunnel = Start-Process -FilePath ssh.exe -ArgumentList @(
        '-N', '-L', '127.0.0.1:55432:127.0.0.1:5432',
        '-o', 'ExitOnForwardFailure=yes', '-o', 'BatchMode=yes',
        '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', 'bloguito'
    ) -WindowStyle Hidden -PassThru
    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 200
        if ($tunnel.HasExited) { throw 'The PostgreSQL SSH tunnel could not start' }
        $listener = Get-NetTCPConnection -LocalPort 55432 -State Listen -ErrorAction SilentlyContinue
    } while (-not $listener -and (Get-Date) -lt $deadline)
    if (-not $listener) { throw 'The PostgreSQL SSH tunnel is unavailable' }
}
$env:TOPIK_DATABASE_URL = $config.database_url
$env:TOPIK_MEDIA_ROOT = $config.media_root
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    & py -3 -B src/review_ui.py --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'The PostgreSQL reviewer stopped with an error' }
} finally {
    Pop-Location
}
