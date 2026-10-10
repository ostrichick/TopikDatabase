# Run REAL PostgreSQL 17 concurrency tests in a disposable LOCAL-only cluster.
# All PostgreSQL provisioning is invoked directly by PowerShell. On this Windows
# workstation initdb succeeded in PowerShell but failed when launched via a
# Python subprocess. Do not touch PostgreSQL services, tunnels or operational DBs.
#
# Usage from C:\Projects\TopikDatabase:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force
#   & .\scripts\verify_shared_pg_concurrency.ps1
# (No machine/user-level PowerShell settings are changed.)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtime = (Resolve-Path -LiteralPath (Join-Path $root '.stage9-runtime')).Path
$bin = Join-Path $runtime 'PostgreSQL\17.11\pgsql\bin'
$initdb = Join-Path $bin 'initdb.exe'
$pgctl = Join-Path $bin 'pg_ctl.exe'
$psql = Join-Path $bin 'psql.exe'
$createdb = Join-Path $bin 'createdb.exe'
$python = Join-Path $root '.venv\Scripts\python.exe'
$source = Join-Path $root 'topik-past-papers\derived\035-I-B.sqlite'
$expectedSha = '076826708a93718faa28c3c1ae3e87bbf28aac4bcfaed88359fd2a12b1c0b7e6'
$report = Join-Path $runtime 'shared_pg_isolated_run.json'
$testLog = Join-Path $runtime 'shared_pg_isolated_test.log'

foreach ($path in @($initdb, $pgctl, $psql, $createdb, $python, $source)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing isolated test prerequisite: $path" }
}
if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedSha) {
    throw 'Frozen 35th SQLite source checksum mismatch. Refuse tests.'
}
if (-not (Test-Path -LiteralPath (Join-Path $root 'tests\test_shared_transcript_postgres_concurrency.py'))) {
    throw 'Missing safety-gated PostgreSQL concurrency test.'
}

$nonce = [guid]::NewGuid().ToString('N').Substring(0, 20)
$port = $null
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    $candidate = 50000 + (Get-Random -Minimum 0 -Maximum 9500)
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([Net.IPAddress]::Parse('127.0.0.1'), $candidate)
        $listener.Start()
        $port = $candidate
        break
    } catch [System.Net.Sockets.SocketException] { } finally {
        if ($null -ne $listener) { $listener.Stop() }
    }
}
if ($null -eq $port) { throw 'No unused high loopback test port available' }

$dbName = 'topik_shared_pg_test_' + $nonce
$directoryName = 'shared-pg-' + [guid]::NewGuid().ToString('N').Substring(0, 16)
$directory = Join-Path $runtime $directoryName
if (Test-Path -LiteralPath $directory) { throw 'Test directory collision' }
$directory = (New-Item -ItemType Directory -Path $directory -ErrorAction Stop).FullName
$data = Join-Path $directory 'data'
$log = Join-Path $directory 'pg.log'
$serverStarted = $false
$serverStopped = $false
$removed = $false
$exitCode = 1
$result = [ordered]@{
    kind = 'fresh-isolated-postgresql-17.11'
    database = $dbName
    port = $port
    frozen_sha256 = $expectedSha
    server_started = $false
    tests_passed = $false
    server_stopped = $false
    disposable_cluster_removed = $false
    elapsed_ms = $null
}
$startedAt = [Diagnostics.Stopwatch]::StartNew()

try {
    & $initdb '-D' $data '-U' 'postgres' '--auth=trust' '--encoding=UTF8' '--locale=C' '--no-instructions' *> (Join-Path $directory 'initdb.log')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $data 'PG_VERSION'))) {
        throw 'Fresh, isolated initdb failed (see test-only initdb.log)'
    }
    # Do NOT redirect pg_ctl stdout/stderr through PowerShell: on Windows the
    # background postgres.exe can retain inherited pipe handles, which blocks
    # the *caller* until the server stops even when pg_ctl reports success.
    & $pgctl '-D' $data '-l' $log '-o' ('-h 127.0.0.1 -p {0} -c max_connections=30' -f $port) '-w' 'start'
    if ($LASTEXITCODE -ne 0) { throw 'Isolated pg_ctl start failed' }
    $serverStarted = $true
    $result.server_started = $true

    $baseArgs = @('-h', '127.0.0.1', '-p', [string]$port, '-U', 'postgres')
    $identity = & $psql @baseArgs '-d' 'postgres' '-Atc' "SELECT current_setting('port'),inet_server_addr(),current_setting('server_version_num')"
    if ($LASTEXITCODE -ne 0 -or ($identity.Trim() -ne ("$port" + '|127.0.0.1|170011'))) {
        throw 'Server identity/version is not the newly started PostgreSQL 17.11 loopback instance'
    }

    & $createdb @baseArgs $dbName
    if ($LASTEXITCODE -ne 0) { throw 'Could not create fresh test-only DB' }
    $commentSql = "COMMENT ON DATABASE $dbName IS 'TOPIK_SHARED_PG_TEST::$nonce'"
    & $psql @baseArgs '-d' 'postgres' '-v' 'ON_ERROR_STOP=1' '-c' $commentSql
    if ($LASTEXITCODE -ne 0) { throw 'Could not set test DB safety marker' }

    # Environment is inherited ONLY by this shell and its Python child.
    # The guarded test additionally checks hostname, high port, database
    # nonce/comment, PG version and empty public schema BEFORE migration.
    $env:TOPIK_SHARED_PG_TEST_URL = "postgresql://postgres@127.0.0.1:$port/$dbName" + '?sslmode=disable'
    $env:TOPIK_SHARED_PG_TEST_MARKER = $nonce
    Remove-Item Env:TOPIK_DATABASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:PGHOST,Env:PGPORT,Env:PGDATABASE,Env:PGUSER,Env:PGPASSWORD -ErrorAction SilentlyContinue
    # unittest -v deliberately prints progress to stderr. In Windows
    # PowerShell 5.1, ErrorActionPreference=Stop misclassifies that normal
    # native stderr as a terminating NativeCommandError, even on exit 0.
    $previousAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $python '-m' 'unittest' 'tests.test_shared_transcript_postgres_concurrency' '-v' 2>&1 |
            ForEach-Object {
                if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message }
                else { [string]$_ }
            } | Out-File -LiteralPath $testLog -Encoding UTF8
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousAction
    }
    Get-Content -LiteralPath $testLog -Tail 32
    if ($code -ne 0) { throw "Isolated PG test suite returned exit $code" }
    $result.tests_passed = $true
    $exitCode = 0
} catch {
    $result.error = $_.Exception.Message
    Write-Output ('ISOLATED_PG_VERIFY_ERROR: ' + $result.error)
} finally {
    if ($serverStarted) {
        & $pgctl '-D' $data '-m' 'fast' '-w' 'stop'
        $serverStopped = $LASTEXITCODE -eq 0
    } else {
        $serverStopped = $true
    }
    $result.server_stopped = $serverStopped

    $resolved = (Resolve-Path -LiteralPath $directory).Path
    if ((Split-Path -Path $resolved -Parent) -ne $runtime -or (Split-Path -Path $resolved -Leaf) -ne $directoryName -or
        -not $directoryName.StartsWith('shared-pg-')) { throw 'Refuse unsafe recursive test cleanup' }
    if ($serverStopped) {
        # Cleanup only the UUID-named directory created in this invocation.
        # No existing test archive, PG binaries, original or operational DB.
        for ($i = 0; $i -lt 5 -and -not $removed; $i++) {
            try {
                Remove-Item -LiteralPath $resolved -Force -Recurse -ErrorAction Stop
                $removed = $true
            } catch { Start-Sleep -Milliseconds 450 }
        }
    }
    $result.disposable_cluster_removed = $removed
    $startedAt.Stop()
    $result.elapsed_ms = [int]$startedAt.ElapsedMilliseconds
    if (-not $serverStopped -or -not $removed) {
        $result.cleanup_blocker = $resolved
        $exitCode = 1
    }
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $report -Encoding UTF8
    Write-Output ('ISOLATED_PG_REPORT=' + $report)
    Write-Output ('ISOLATED_PG_PASS=' + $result.tests_passed + '; STOPPED=' + $serverStopped + '; REMOVED=' + $removed)
}
exit $exitCode
