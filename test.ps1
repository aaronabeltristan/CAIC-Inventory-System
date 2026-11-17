<#
.SYNOPSIS
    Checks the whole project before a deploy: backend tests, sample data, backup script and
    the frontend build. Everything runs in Docker; nothing on this computer is changed.

.DESCRIPTION
    Checks, in order:
      1. Backend tests   Backend/tests with pytest, against a THROWAWAY Postgres database
                         (also imports the files in sample_data/ to check they still work)
      2. Backup script   Backend/backup/backup.sh has no shell syntax errors
      3. Frontend build  the React app builds (npm ci + vite build), like a deploy does
      4. Frontend lint   ESLint report (shown, not counted, unless -StrictLint)

    The real database (postgres-db / caic_db), the running app and Backend\.env are never used.
    Project folders are mounted read-only. Safe to run at any time, even while the app is live.
    The first run downloads images and packages (a few minutes); later runs reuse them.

.EXAMPLE
    .\test.ps1
    Everything.

.EXAMPLE
    .\test.ps1 -k sessions
    Only backend tests with "sessions" in their name (any pytest options can follow; giving
    pytest options skips the frontend checks unless you add -Frontend).

.EXAMPLE
    .\test.ps1 -Backend
    Only the backend tests and the backup script (fast).

.EXAMPLE
    .\test.ps1 -Frontend
    Only the frontend build and lint.

.EXAMPLE
    .\test.ps1 -- -x -v -k "settings and not logo"
    Put pytest options after -- when they could be mistaken for this script's own switches
    or PowerShell's (-v would mean -Verbose, -s would mean -StrictLint). -k and -x work without it.
#>
param(
    [switch]$Backend,      # only the backend tests and the backup script
    [switch]$Frontend,     # only the frontend build and lint
    [switch]$StrictLint,   # count ESLint errors as a failure
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$PytestArgs
)

# Not "Stop": in Windows PowerShell, docker writing to stderr would then count as a failure
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$backendDir = Join-Path $root "Backend"
$frontendDir = Join-Path $root "Frontend"
$sampleDir = Join-Path $root "sample_data"

# Must match production (Backend/Dockerfile, Frontend/Dockerfile, docker-compose.yml)
$pythonImage = "python:3.13-slim"
$postgresImage = "postgres:16-alpine"
$nodeImage = "node:22-alpine"

# A new name each run, so a container left by an interrupted run can't get in the way
$run = "caic-test-" + (Get-Random -Minimum 10000 -Maximum 99999)
$net = "$run-net"
$db = "$run-db"

$PytestArgs = @($PytestArgs | Where-Object { $_ })
$runBackend = $Backend -or -not $Frontend
$runFrontend = $Frontend -or (-not $Backend -and $PytestArgs.Count -eq 0)

$results = [System.Collections.Generic.List[object]]::new()
function Add-Result($name, $status, $seconds, $note = "") {
    $results.Add([pscustomobject]@{ Check = $name; Result = $status; Seconds = [math]::Round($seconds); Note = $note })
}
function Write-Step($text) { Write-Host "`n== $text ==" -ForegroundColor Cyan }

function Remove-TestContainers {
    docker rm -f $db 2>$null | Out-Null
    docker network rm $net 2>$null | Out-Null
}
# Leftovers from runs that were interrupted (older versions of this script used fixed names)
function Remove-Leftovers {
    $old = @(docker ps -aq --filter "name=caic-test-" 2>$null) + @(docker ps -aq --filter "name=^caic-test-db$" 2>$null)
    $old | Where-Object { $_ } | ForEach-Object { docker rm -f $_ 2>$null | Out-Null }
    @(docker network ls -q --filter "name=caic-test-" 2>$null) | Where-Object { $_ } |
        ForEach-Object { docker network rm $_ 2>$null | Out-Null }
}

docker version --format "{{.Server.Version}}" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "Docker isn't running. Start Docker Desktop and try again." -ForegroundColor Red; exit 1 }
Remove-Leftovers

# ---------- 1. Backend tests ----------
if ($runBackend) {
    Write-Step "Backend tests (pytest, throwaway database)"
    $t = [Diagnostics.Stopwatch]::StartNew()
    $status = "FAILED"; $note = ""
    try {
        docker network create $net | Out-Null
        docker run -d --rm --name $db --network $net `
            -e POSTGRES_PASSWORD=test -e POSTGRES_DB=caic_db_test `
            $postgresImage -c fsync=off -c synchronous_commit=off -c full_page_writes=off | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Couldn't start the test database container." }

        $ready = $false
        foreach ($i in 1..60) {
            docker exec $db pg_isready -U postgres -d caic_db_test 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $ready = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $ready) { throw "The test database didn't start." }

        $mounts = @("-v", "${backendDir}:/app:ro")
        if (Test-Path $sampleDir) { $mounts += @("-v", "${sampleDir}:/sample_data:ro") }
        else { $note = "sample_data missing: its tests were skipped" }

        # Pytest options go in PYTEST_ADDOPTS (pytest splits it like a shell), quoting the ones
        # with spaces in single quotes. No double quotes are passed to docker: Windows
        # PowerShell 5.1 would split them wrongly.
        $addopts = ($PytestArgs | ForEach-Object {
            if ($_ -match "\s") { "'" + ($_ -replace "'", "") + "'" } else { $_ }
        }) -join " "
        $script = "pip install --quiet --disable-pip-version-check --root-user-action=ignore -r requirements-dev.txt " +
                  "&& python -m pytest -q -p no:cacheprovider"
        docker run --rm --network $net @mounts -w /app `
            -v caic-test-pip-cache:/root/.cache/pip `
            -e PYTHONDONTWRITEBYTECODE=1 `
            -e "TEST_DATABASE_URL=postgresql+psycopg://postgres:test@${db}:5432/caic_db_test" `
            -e SAMPLE_DATA_DIR=/sample_data `
            -e "PYTEST_ADDOPTS=$addopts" `
            $pythonImage sh -c $script
        $code = $LASTEXITCODE
        if ($code -eq 0) { $status = "passed" }
        elseif ($code -eq 5) { $status = "no tests"; $note = "no test matched the options given" }
    }
    catch { $note = $_.Exception.Message; Write-Host $note -ForegroundColor Red }
    finally { Remove-TestContainers }
    Add-Result "Backend tests" $status $t.Elapsed.TotalSeconds $note

    # ---------- 2. Backup script ----------
    Write-Step "Backup script (shell syntax)"
    $t = [Diagnostics.Stopwatch]::StartNew()
    # Strips Windows line endings first, as Backend/backup/Dockerfile does when building the image
    docker run --rm -v "${backendDir}/backup:/b:ro" $postgresImage sh -c "tr -d '\r' < /b/backup.sh | sh -n"
    Add-Result "Backup script" $(if ($LASTEXITCODE -eq 0) { "passed" } else { "FAILED" }) $t.Elapsed.TotalSeconds
}

# ---------- 3 and 4. Frontend build and lint ----------
if ($runFrontend) {
    # The source is copied inside the container (without the Windows node_modules, which don't
    # work on Linux), so the build never writes into the project. npm's download cache is kept.
    $prepare = 'mkdir -p /work && cd /src && tar cf - --exclude=./node_modules --exclude=./dist . | tar xf - -C /work ' +
               '&& cd /work && npm ci --no-audit --no-fund --loglevel=error'

    Write-Step "Frontend build (npm ci + vite build)"
    $t = [Diagnostics.Stopwatch]::StartNew()
    $buildScript = "$prepare && npx vite build --logLevel warn --outDir /tmp/dist --emptyOutDir && echo BUILD-OK" +
                   ' && echo && echo == ESLint report == && (npx eslint . --format stylish; echo LINT-EXIT=$?)'
    # Shown as it happens, and kept to read the results
    docker run --rm -v "${frontendDir}:/src:ro" -v caic-test-npm-cache:/root/.npm `
        -e CI=true $nodeImage sh -c $buildScript 2>&1 |
        Tee-Object -Variable output | ForEach-Object { Write-Host "$_" }
    $text = ($output | Out-String)
    $built = $text -match "BUILD-OK"
    Add-Result "Frontend build" $(if ($built) { "passed" } else { "FAILED" }) $t.Elapsed.TotalSeconds

    if ($built) {
        $lintExit = if ($text -match "LINT-EXIT=(\d+)") { [int]$Matches[1] } else { 2 }
        $problems = if ($text -match "(\d+) problems? \((\d+) errors?") { "$($Matches[2]) error(s)" } else { "" }
        if ($lintExit -eq 0) { Add-Result "Frontend lint" "passed" 0 }
        elseif ($StrictLint) { Add-Result "Frontend lint" "FAILED" 0 $problems }
        else { Add-Result "Frontend lint" "warnings" 0 "$problems (not counted; -StrictLint to count)" }
    }
    else { Add-Result "Frontend lint" "skipped" 0 "the build failed" }
}

# ---------- Summary ----------
Write-Host "`n== Summary ==" -ForegroundColor Cyan
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { $_.Result -eq "FAILED" -or $_.Result -eq "no tests" })
if ($failed.Count -eq 0) { Write-Host "All checks passed." -ForegroundColor Green; exit 0 }
Write-Host "Failed: $($failed.Check -join ', ') (see above)." -ForegroundColor Red
exit 1
