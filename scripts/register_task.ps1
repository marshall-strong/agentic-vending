<#
.SYNOPSIS
    Register (or remove) the Windows scheduled tasks that keep the snapshot
    archive filled.

.DESCRIPTION
    Creates two tasks:

      "DEX snapshot archive"        polls the machine every 30 minutes between
                                    07:37 and 18:37 local time, every day.

      "DEX snapshot archive check"  runs once a day at 18:50 and reports
                                    whether the archive is current. This is the
                                    thing that turns a silently dead scheduler
                                    into a visible one.

    The defaults are explained in docs\snapshot-archive.md. If you change the
    cadence here, change WINDOW_START / WINDOW_END / INTERVAL_MINUTES in
    scripts\check_archive.py too, or the health check will disagree with
    reality.

.PARAMETER RunAsSystem
    Register the tasks to run as SYSTEM instead of as you. SYSTEM runs whether
    or not anyone is logged in, which is what you want for an archive that must
    not miss days -- but registering it requires an elevated PowerShell.

    Without this switch the tasks run as the current user and only fire while
    that user is logged on. No password is stored in either case.

.PARAMETER Unregister
    Remove both tasks and exit.

.EXAMPLE
    .\scripts\register_task.ps1
    Register both tasks to run as the current user.

.EXAMPLE
    .\scripts\register_task.ps1 -RunAsSystem
    Register both tasks to run as SYSTEM. Needs an elevated PowerShell.
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'DEX snapshot archive',
    [string]$CheckTaskName = 'DEX snapshot archive check',
    [string]$StartTime = '07:37',
    [int]$IntervalMinutes = 30,
    [int]$DurationHours = 11,
    [string]$CheckTime = '18:50',
    [switch]$RunAsSystem,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PollWrapper = Join-Path $PSScriptRoot 'run_poll.ps1'
$CheckWrapper = Join-Path $PSScriptRoot 'run_check.ps1'

if ($Unregister) {
    foreach ($name in @($TaskName, $CheckTaskName)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Output "Removed scheduled task: $name"
        } else {
            Write-Output "No such scheduled task: $name"
        }
    }
    exit 0
}

if (-not (Test-Path $PollWrapper)) { throw "Missing $PollWrapper" }
if (-not (Test-Path (Join-Path $RepoRoot '.env'))) {
    Write-Warning "$RepoRoot\.env does not exist. Copy .env.example to .env and fill it in, or every run will fail with exit code 2."
}

# Run as SYSTEM (survives logout) or as the current user (needs a login
# session). Neither stores a password.
if ($RunAsSystem) {
    $isElevated = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isElevated) {
        throw 'Registering a SYSTEM task needs an elevated PowerShell. Re-run "Run as administrator", or drop -RunAsSystem to register it as yourself.'
    }
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $accountNote = 'SYSTEM (runs whether or not anyone is logged in)'
} else {
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $accountNote = "$env:USERDOMAIN\$env:USERNAME (runs only while this user is logged on)"
}

# -StartWhenAvailable is the important one: if the machine was asleep at 10:07,
# the task fires as soon as it wakes instead of skipping the slot entirely. A
# late snapshot is worth far more than a missing one, because what we are
# building is a series of differences.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

function New-WrapperAction([string]$WrapperPath) {
    New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$WrapperPath`"" `
        -WorkingDirectory $RepoRoot
}

# --- The polling task ------------------------------------------------------
# The trigger has to be -Daily, not -Once. A -Once trigger with a repetition
# runs the repetition exactly once and then never fires again: the task
# registers cleanly, reports "Ready", and quietly has no next run time. That is
# precisely the kind of silent failure this whole archive is built to avoid.
#
# PowerShell will not build a daily trigger with a repetition directly, so we
# borrow the Repetition object off a throwaway -Once trigger. This is the
# standard workaround, and the verification below proves it took.
$pollTrigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$pollTrigger.Repetition = (
    New-ScheduledTaskTrigger -Once -At $StartTime `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
        -RepetitionDuration (New-TimeSpan -Hours $DurationHours)
).Repetition

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Polls the DEX Bridge and archives the raw response to data\snapshots. See docs\snapshot-archive.md." `
    -Action (New-WrapperAction $PollWrapper) `
    -Trigger $pollTrigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

# --- The daily health check ------------------------------------------------
Register-ScheduledTask `
    -TaskName $CheckTaskName `
    -Description "Reports whether the snapshot archive is current. Exit code 1 means the archive has gone stale." `
    -Action (New-WrapperAction $CheckWrapper) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $CheckTime) `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

# --- Verify what actually got registered -----------------------------------
# Registering a task can succeed while producing a task that never runs, so we
# read it back rather than trusting that it worked. Better to fail here than to
# discover a week later that the archive has a week-shaped hole in it.
$registered = Get-ScheduledTask -TaskName $TaskName
$registeredTrigger = $registered.Triggers[0]
$expectedInterval = "PT$($IntervalMinutes)M"
$expectedDuration = "PT$($DurationHours)H"

if ($registeredTrigger.Repetition.Interval -ne $expectedInterval) {
    throw "Registration did not take: repetition interval is '$($registeredTrigger.Repetition.Interval)', expected '$expectedInterval'."
}
if ($registeredTrigger.Repetition.Duration -ne $expectedDuration) {
    throw "Registration did not take: repetition duration is '$($registeredTrigger.Repetition.Duration)', expected '$expectedDuration'."
}

$nextRun = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
if (-not $nextRun) {
    throw "Registration did not take: '$TaskName' has no next run time, so it would never fire. Check that the task is enabled."
}

$pollsPerDay = [int]([math]::Floor($DurationHours * 60 / $IntervalMinutes)) + 1
$endTime = ([datetime]$StartTime).AddHours($DurationHours).ToString('HH:mm')

Write-Output ''
Write-Output "Registered: $TaskName"
Write-Output "  Runs       : every $IntervalMinutes minutes from $StartTime to $endTime, every day"
Write-Output "  That is    : $pollsPerDay polls a day"
Write-Output "  Next run   : $nextRun"
Write-Output "  Account    : $accountNote"
Write-Output "  Command    : powershell.exe -File `"$PollWrapper`""
Write-Output ''
Write-Output "Registered: $CheckTaskName"
Write-Output "  Runs       : daily at $CheckTime"
Write-Output ''
Write-Output 'Check on them with:'
Write-Output "  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Output ''
Write-Output 'Run one right now without waiting for the schedule:'
Write-Output "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Output ''
Write-Output 'Remove both again with:'
Write-Output '  .\scripts\register_task.ps1 -Unregister'
