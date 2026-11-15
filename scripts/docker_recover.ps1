<#
.SYNOPSIS
    Check that Docker Desktop can start a container. If it can't, recover it.

.DESCRIPTION
    Run this first whenever Docker hangs on this Windows laptop. Don't improvise.
    See docs/docker-recovery.md for the cause and what each step does.

    1. Probe: `docker version`, then `docker run --rm alpine:3 true`, each with a timeout.
       If both pass, the engine is healthy and the script stops (exit 0).
    2. Recover, every step with a timeout:
         docker desktop stop --force   (if that hangs: kill the Docker Desktop processes)
         wsl --shutdown
         wsl -d docker-desktop -e true (the WSL service can boot a VM again)
         docker desktop start, then wait for the engine
    3. Probe again. Restart any container that was running before and isn't now,
       and run the KsaiWslKeepalive task (WSL sshd for `ssh laptop`) if it exists.
    4. Remove leftover scratch containers: label agentic-vending.scratch=true, state
       "created" (never started), older than 10 minutes. Other stuck containers are
       listed, not touched.

    Every run appends one line to data\logs\docker-recover.log.

.PARAMETER CheckOnly
    Probe only. Never stop or restart anything.

.PARAMETER DryRun
    Print the recovery commands instead of running them. Probes still run for real.

.PARAMETER SimulateHang
    Treat the first probe as failed. Use it with -DryRun to test the escalation path
    without stopping anything.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\docker_recover.ps1

.NOTES
    Exit codes: 0 = Docker can run containers (healthy, or recovered).
                1 = still broken after recovery (or hung, with -CheckOnly).
                2 = the WSL service itself is wedged: needs an elevated
                    `Restart-Service WSLService`, or a reboot.
                3 = the probe image is missing and can't be pulled.
                4 = the docker CLI is not on PATH.
#>
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$DryRun,
    [switch]$SimulateHang,
    [int]$ProbeTimeoutSec = 60,
    [int]$StopTimeoutSec = 120,
    [int]$WslShutdownTimeoutSec = 120,
    [int]$WslBootTimeoutSec = 60,
    [int]$StartTimeoutSec = 240,
    [string]$ProbeImage = 'alpine:3'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$ScratchLabel = 'agentic-vending.scratch=true'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot 'data\logs'
$RunLog = Join-Path $LogDir 'docker-recover.log'
$Started = Get-Date

function Say([string]$msg) {
    # Write-Host, not Write-Output: output would leak into function return values.
    Write-Host ('[{0:HH:mm:ss}] {1}' -f (Get-Date), $msg)
}

function Write-RunLog([string]$outcome, [string]$detail) {
    try {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        $secs = [int]((Get-Date) - $Started).TotalSeconds
        $line = "$stamp outcome=$outcome seconds=$secs dryrun=$([bool]$DryRun) detail=""$detail"""
        Add-Content -Path $RunLog -Value $line -Encoding utf8
    } catch {
        Say "could not write $RunLog : $_"
    }
}

function Finish([int]$code, [string]$outcome, [string]$detail) {
    Say "RESULT: $outcome - $detail"
    Write-RunLog $outcome $detail
    exit $code
}

# Run a program with a hard timeout. On timeout, kill it and its child processes.
# Returns Ok, TimedOut, ExitCode, Seconds, Output.
function Invoke-Timed([string]$File, [string[]]$Arguments, [int]$TimeoutSec) {
    $quoted = $Arguments | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $File
    $psi.Arguments = ($quoted -join ' ')
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables['WSL_UTF8'] = '1'   # wsl.exe prints UTF-16 otherwise

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
    } catch {
        return [pscustomobject]@{ Ok = $false; TimedOut = $false; ExitCode = -1; Seconds = 0; Output = "$_" }
    }
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    $exited = $p.WaitForExit($TimeoutSec * 1000)
    if (-not $exited) {
        & taskkill.exe /T /F /PID $p.Id 2>&1 | Out-Null
        $p.WaitForExit(5000) | Out-Null
    }
    $sw.Stop()
    # A process that leaves a child holding the pipe (docker desktop start does) never
    # closes its output, so only wait briefly for it.
    $out = ''
    if ($outTask.Wait(2000)) { $out += $outTask.Result }
    if ($errTask.Wait(2000)) { $out += $errTask.Result }
    $code = if ($exited) { $p.ExitCode } else { -1 }
    return [pscustomobject]@{
        Ok       = ($exited -and $code -eq 0)
        TimedOut = (-not $exited)
        ExitCode = $code
        Seconds  = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        Output   = ($out -replace "`0", '').Trim()
    }
}

function Describe($r) {
    if ($r.TimedOut) { return "TIMED OUT after $($r.Seconds)s" }
    if ($r.Ok) { return "ok in $($r.Seconds)s" }
    $first = ($r.Output -split "`n" | Select-Object -First 2) -join ' | '
    return "failed (exit $($r.ExitCode)) in $($r.Seconds)s: $first"
}

# A destructive step: in -DryRun, print it instead of running it.
function Invoke-Step([string]$name, [string]$File, [string[]]$Arguments, [int]$TimeoutSec) {
    if ($DryRun) {
        Say "DRY RUN, would run (timeout ${TimeoutSec}s): $File $($Arguments -join ' ')"
        return [pscustomobject]@{ Ok = $true; TimedOut = $false; ExitCode = 0; Seconds = 0; Output = '' }
    }
    Say "$name : $File $($Arguments -join ' ')  (timeout ${TimeoutSec}s)"
    $r = Invoke-Timed $File $Arguments $TimeoutSec
    Say "$name : $(Describe $r)"
    return $r
}

# The engine answers AND can create, start and remove a container.
function Test-Engine([string]$label) {
    $v = Invoke-Timed 'docker' @('version', '--format', '{{.Server.Version}}') 20
    if (-not $v.Ok) {
        Say "$label probe: engine API $(Describe $v)"
        return $false
    }
    $run = Invoke-Timed 'docker' @('run', '--rm', '--pull', 'never', '--label', $ScratchLabel,
        $ProbeImage, 'true') $ProbeTimeoutSec
    Say "$label probe: engine $($v.Output); docker run --rm $ProbeImage true $(Describe $run)"
    return $run.Ok
}

function Get-RunningNames {
    $r = Invoke-Timed 'docker' @('ps', '--format', '{{.Names}}') 20
    if (-not $r.Ok) { return $null }
    # The leading comma keeps an empty or one-item list an array on return.
    return ,@($r.Output -split "`r?`n" | Where-Object { $_ })
}

function Wait-Engine([int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $v = Invoke-Timed 'docker' @('version', '--format', '{{.Server.Version}}') 15
        if ($v.Ok) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

# Remove scratch containers stuck in "created" for more than 10 minutes.
# Report other stuck containers without touching them.
function Clear-StuckContainers {
    $r = Invoke-Timed 'docker' @('ps', '-a', '--filter', 'status=created',
        '--format', '{{.ID}}|{{.Names}}|{{.CreatedAt}}|{{.Label "agentic-vending.scratch"}}') 20
    if (-not $r.Ok) { Say "cleanup: could not list containers: $(Describe $r)"; return }
    $cutoff = (Get-Date).AddMinutes(-10)
    foreach ($line in ($r.Output -split "`r?`n" | Where-Object { $_ })) {
        $id, $name, $created, $scratch = $line -split '\|', 4
        # CreatedAt looks like "2026-11-14 18:07:17 -0500 EST"
        $when = $null
        try {
            $parts = $created -split ' '
            $tz = $parts[2].Insert(3, ':')   # -0500 -> -05:00
            $when = [datetimeoffset]::ParseExact("$($parts[0]) $($parts[1]) $tz", 'yyyy-MM-dd HH:mm:ss zzz',
                [Globalization.CultureInfo]::InvariantCulture).LocalDateTime
        } catch { }
        $old = ($null -ne $when -and $when -lt $cutoff)
        if ($scratch -eq 'true' -and $old) {
            $rm = Invoke-Step 'cleanup' 'docker' @('rm', '-v', $id) 30
            if ($rm.Ok) { Say "cleanup: removed stuck scratch container $name (created $created)" }
        } elseif ($old) {
            Say "cleanup: $name is stuck in Created since $created and has no scratch label. Not touched; look at it, then 'docker rm -v $name'."
        }
    }
}

# --- 0. Preflight ---------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Finish 4 'no_docker_cli' 'docker is not on PATH'
}
$img = Invoke-Timed 'docker' @('image', 'inspect', '--format', '{{.Id}}', $ProbeImage) 20
if (-not $img.Ok -and -not $img.TimedOut) {
    Say "probe image $ProbeImage is not cached; pulling it"
    $pull = Invoke-Timed 'docker' @('pull', $ProbeImage) 180
    if (-not $pull.Ok) {
        Finish 3 'no_probe_image' "docker pull $ProbeImage $(Describe $pull)"
    }
}

# --- 1. Probe ------------------------------------------------------------------
$healthy = Test-Engine 'first'
if ($SimulateHang) {
    Say 'first probe: -SimulateHang set, treating it as failed'
    $healthy = $false
}
if ($healthy) {
    Clear-StuckContainers
    Finish 0 'healthy' 'docker run worked; nothing restarted'
}
if ($CheckOnly) {
    Finish 1 'hung' 'docker run failed; -CheckOnly, so nothing restarted'
}

# --- 2. Recover ----------------------------------------------------------------
$before = Get-RunningNames
if ($null -eq $before) {
    Say 'could not list running containers (engine not answering); will not restore any'
    $before = @()
} else {
    Say "running before recovery: $(if ($before.Count) { $before -join ', ' } else { '(none)' })"
}

$stop = Invoke-Step 'stop' 'docker' @('desktop', 'stop', '--force', '--timeout', "$StopTimeoutSec") ($StopTimeoutSec + 15)
if (-not $stop.Ok -and -not $DryRun) {
    Say 'stop: did not finish; killing the Docker Desktop processes'
    Get-Process -Name 'Docker Desktop', 'com.docker.backend', 'com.docker.build', 'com.docker.dev-envs' `
        -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

$shutdown = Invoke-Step 'wsl-shutdown' 'wsl.exe' @('--shutdown') $WslShutdownTimeoutSec
$boot = $null
if ($shutdown.Ok) {
    $boot = Invoke-Step 'wsl-boot' 'wsl.exe' @('-d', 'docker-desktop', '-e', 'true') $WslBootTimeoutSec
}
if (-not $shutdown.Ok -or -not $boot.Ok) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        Say 'WSL is wedged; restarting WSLService (elevated)'
        Restart-Service -Name WSLService -Force
        $boot = Invoke-Step 'wsl-boot' 'wsl.exe' @('-d', 'docker-desktop', '-e', 'true') $WslBootTimeoutSec
    }
    if ($boot -eq $null -or -not $boot.Ok) {
        Finish 2 'wsl_wedged' ('WSL could not shut down or boot. In an elevated PowerShell run ' +
            '"Restart-Service WSLService -Force", then run this script again. If that hangs too, reboot.')
    }
}

$start = Invoke-Step 'start' 'docker' @('desktop', 'start', '--timeout', "$StartTimeoutSec") ($StartTimeoutSec + 15)
if (-not $DryRun -and -not (Wait-Engine 120)) {
    Finish 1 'engine_down' "Docker Desktop did not answer within 120s after start ($(Describe $start))"
}

# Kevin's WSL sshd (`ssh laptop`) went down with `wsl --shutdown`; bring it back now.
if (Get-ScheduledTask -TaskName 'KsaiWslKeepalive' -ErrorAction SilentlyContinue) {
    if ($DryRun) { Say 'DRY RUN, would run scheduled task KsaiWslKeepalive' }
    else { Start-ScheduledTask -TaskName 'KsaiWslKeepalive'; Say 'started scheduled task KsaiWslKeepalive (WSL sshd)' }
}

# --- 3. Probe again, restore containers ------------------------------------------
if (-not (Test-Engine 'second')) {
    Finish 1 'still_hung' 'docker run still fails after the full restart. Collect `docker desktop diagnose` and see docs/docker-recovery.md.'
}

$after = Get-RunningNames
if ($null -eq $after) { $after = @() }
$restarted = @()
foreach ($name in $before) {
    if ($after -notcontains $name) {
        # Skip containers that are gone or were started with --rm: they aren't coming back.
        $ar = Invoke-Timed 'docker' @('inspect', '--format', '{{.HostConfig.AutoRemove}}', $name) 20
        if (-not $ar.Ok -or $ar.Output -eq 'true') { Say "restore: skipping $name (gone, or a --rm container)"; continue }
        $r = Invoke-Step "restore $name" 'docker' @('start', $name) 60
        if ($r.Ok -and -not $DryRun) { $restarted += $name }
    }
}
if ($restarted.Count) { Say "restarted: $($restarted -join ', ')" }

Clear-StuckContainers
$detail = if ($DryRun) { 'dry run: escalation path printed, nothing stopped' } else { 'docker run works again after stop + wsl --shutdown + start' }
Finish 0 'recovered' $detail
