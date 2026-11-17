<#
.SYNOPSIS
    Causes one of the planned faults on the server and times it: when it started, when it was
    noticed (MTTD) and when it was fixed (MTTR). See docs/FAULT_TESTS.md.

.DESCRIPTION
    1. Records the start time (T0) and causes the fault (or waits for you to cause it, with -Manual).
    2. Watches http://localhost/api/health every second and notes when it changes (the
       monitoring's own detection and recovery times).
    3. You press D the moment a person notices the problem (from a symptom on screen, a status
       light, an error code...), then R once it's repaired and the app works again.
       Q stops without a result.
    4. Appends one row to docs\fault-results.csv and prints MTTD and MTTR for this run.

    Run on the server, in PowerShell, from the project folder. Causing a fault interrupts the
    app for everyone: do it outside working hours, and tell users first.

.EXAMPLE
    .\tools\fault-timer.ps1 -Fault backend -Run 1 -Detector "Ana" -Repairer "Ben"
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("backend", "database", "backup", "assistant", "crash")]
    [string]$Fault,
    [int]$Run = 1,
    [string]$Detector = "",
    [string]$Repairer = "",
    [switch]$Manual,           # don't cause the fault: you cause it yourself when prompted
    [string]$HealthUrl = "http://localhost/api/health"
)
$ErrorActionPreference = "Continue"
$root = Split-Path $PSScriptRoot -Parent
$results = Join-Path $root "docs\fault-results.csv"

$inject = @{
    backend   = { docker stop caic-api | Out-Null }
    database  = { docker stop postgres-db | Out-Null }
    backup    = { docker stop caic-backup | Out-Null; Write-Host "Now click Back up now in Settings → System." -ForegroundColor Yellow }
    assistant = { docker stop caic-ollama | Out-Null }
    # Ends the backend's main process from inside, like a crash: Docker's restart policy starts it again
    crash     = { docker exec caic-api python -c "import os, signal; os.kill(1, signal.SIGTERM)" 2>$null | Out-Null }
}
$describe = @{
    backend   = "F1 Backend stopped (docker stop caic-api)"
    database  = "F2 Database stopped (docker stop postgres-db)"
    backup    = "F3 Backup service stopped (docker stop caic-backup + Back up now)"
    assistant = "F4 AI model server stopped (docker stop caic-ollama)"
    crash     = "F5 Backend crash, automatic recovery (kill the backend process)"
}

function Get-Health {
    try {
        $r = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 3
        return "ok ($($r.StatusCode))"
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        if ($code) { return "fail (HTTP $code)" }
        return "fail (no answer)"
    }
}
function Stamp($t) { $t.ToString("HH:mm:ss.fff") }

Write-Host "`n$($describe[$Fault]), run $Run" -ForegroundColor Cyan
$before = Get-Health
Write-Host "Health before: $before"
if ($before -notlike "ok*" -and $Fault -ne "backup" -and $Fault -ne "assistant") {
    Write-Host "The system isn't healthy before the test. Fix that first." -ForegroundColor Red; exit 1
}

if ($Manual) { Read-Host "Press Enter at the exact moment you cause the fault" | Out-Null }
$t0 = Get-Date
if (-not $Manual) { & $inject[$Fault] }
Write-Host "T0 fault started  $(Stamp $t0)" -ForegroundColor Yellow
Write-Host "Press D when a person notices the problem, R when it's repaired and working, Q to quit."

$monitorDown = $null; $monitorUp = $null; $detected = $null; $repaired = $null; $signal = ""
$last = $before
while ($true) {
    $h = Get-Health
    $now = Get-Date
    if ($h -ne $last) {
        Write-Host "  $(Stamp $now)  health: $h  (+$([math]::Round(($now - $t0).TotalSeconds, 1)) s)"
        if ($h -notlike "ok*" -and -not $monitorDown) { $monitorDown = $now }
        if ($h -like "ok*" -and $monitorDown -and -not $monitorUp) { $monitorUp = $now }
        $last = $h
    }
    if ([Console]::KeyAvailable) {
        $key = [Console]::ReadKey($true).Key
        if ($key -eq "Q") { Write-Host "Stopped; nothing recorded."; exit 1 }
        if ($key -eq "D" -and -not $detected) {
            $detected = Get-Date
            Write-Host "  $(Stamp $detected)  DETECTED (+$([math]::Round(($detected - $t0).TotalSeconds, 1)) s)" -ForegroundColor Yellow
            $signal = Read-Host "  How was it noticed? (e.g. E-NET-02 on screen, Status red E-DB-01, Live dot)"
            Write-Host "  Now repair it. Press R when the app works again."
        }
        elseif ($key -eq "R" -and $detected) {
            $repaired = Get-Date
            Write-Host "  $(Stamp $repaired)  REPAIRED" -ForegroundColor Green
            break
        }
    }
    # The automatic-recovery test ends by itself once the monitor sees it healthy again
    if ($Fault -eq "crash" -and $monitorUp -and -not $repaired) {
        if (-not $detected) { $detected = $monitorDown; $signal = "health check (automatic)" }
        $repaired = $monitorUp
        Write-Host "  Recovered by itself." -ForegroundColor Green
        break
    }
    Start-Sleep -Milliseconds 1000
}

$mttd = [math]::Round(($detected - $t0).TotalSeconds, 1)
$mttr = [math]::Round(($repaired - $detected).TotalSeconds, 1)
$down = [math]::Round(($repaired - $t0).TotalSeconds, 1)
$monD = if ($monitorDown) { [math]::Round(($monitorDown - $t0).TotalSeconds, 1) } else { "" }
$tools = Read-Host "Tools used to diagnose and repair (e.g. Settings→Status, docker ps, docker logs)"
$steps = Read-Host "Repair steps taken (short)"

if (-not (Test-Path $results)) {
    "fault,run,t0,detected,repaired,mttd_s,mttr_s,downtime_s,monitor_detect_s,signal,detector,repairer,tools,steps" |
        Out-File -FilePath $results -Encoding utf8
}
$csv = { param($v) '"' + (($v -as [string]) -replace '"', "'") + '"' }
($Fault, $Run, $t0.ToString("s"), $detected.ToString("s"), $repaired.ToString("s"), $mttd, $mttr, $down, $monD,
  $signal, $Detector, $Repairer, $tools, $steps | ForEach-Object { & $csv $_ }) -join "," |
    Out-File -FilePath $results -Append -Encoding utf8

Write-Host "`nMTTD $mttd s   MTTR $mttr s   downtime $down s   (monitor saw it after $monD s)" -ForegroundColor Cyan
Write-Host "Saved to $results"
