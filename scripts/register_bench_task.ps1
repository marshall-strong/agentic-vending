<#
.SYNOPSIS
    Register (or remove) a Windows scheduled task that runs the standing benchmark every night.

.DESCRIPTION
    Creates "Vending benchmark", which runs scripts\run_bench.ps1 -Yes daily at 02:00 as you,
    while you're logged on (the key is in your profile, and no password is stored). Each run
    spends real money from the team key: about $1-2 at the default three configs, never more than
    the run's --budget ($5 by default) or what's left of the team's daily test cap. Output goes to
    data\logs\bench-<date>.log; the results go to the database (python -m vending.bench report).

    A person has to run this, and type yes: an unattended spend should be someone's decision.

    Extra arguments for every nightly run go in -BenchArgs, such as
    -BenchArgs '--configs "haiku@medium,opus@low"'.

.PARAMETER Unregister
    Remove the task and exit.

.EXAMPLE
    .\scripts\register_bench_task.ps1
.EXAMPLE
    .\scripts\register_bench_task.ps1 -Unregister
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'Vending benchmark',
    [string]$At = '02:00',
    [string]$BenchArgs = '',
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot 'run_bench.ps1'

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task: $TaskName"
    } else {
        Write-Output "No such scheduled task: $TaskName"
    }
    exit 0
}

if (-not [Environment]::UserInteractive -or ([Environment]::GetCommandLineArgs() -contains '-NonInteractive')) {
    throw 'Run this yourself, in an interactive PowerShell: it sets up a nightly spend on the team key.'
}
if (-not (Test-Path $Runner)) { throw "Missing $Runner" }
if (-not (Test-Path (Join-Path $RepoRoot '.venv\Scripts\python.exe'))) {
    throw "No venv at $RepoRoot\.venv. Create it first (see README), or every run will fail."
}
if (-not (Test-Path (Join-Path $HOME '.vending\anthropic-key')) -and
    -not [Environment]::GetEnvironmentVariable('VENDING_ANTHROPIC_API_KEY', 'User')) {
    Write-Warning ("No key yet: no $HOME\.vending\anthropic-key and no VENDING_ANTHROPIC_API_KEY user variable. " +
                   "Every run will stop before spending until there is one (see the README).")
}

Write-Output "This runs the benchmark every day at $At as $env:USERDOMAIN\$env:USERNAME, spending from the team key"
Write-Output 'within each run''s budget and the team''s daily test cap.'
$answer = Read-Host 'Register it? Type yes'
if ($answer -ne 'yes') { Write-Output 'Not registered.'; exit 0 }

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
# A run missed while the machine slept runs when it wakes; never two at once; a stuck run is stopped.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Runner`" -Yes $BenchArgs").Trim() `
    -WorkingDirectory $RepoRoot

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description 'Runs the standing benchmark (python -m vending.bench run) on the team key. See docs\benchmark.md.' `
    -Action $action `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $At) `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

# Read it back: a task can register cleanly and still never fire.
$nextRun = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
if (-not $nextRun) {
    throw "Registration did not take: '$TaskName' has no next run time. Check that the task is enabled."
}

Write-Output ''
Write-Output "Registered: $TaskName"
Write-Output "  Runs     : daily at $At, this machine's local time"
Write-Output "  Next run : $nextRun"
Write-Output "  Command  : powershell.exe -File `"$Runner`" -Yes $BenchArgs"
Write-Output "  Log      : $RepoRoot\data\logs\bench-<date>.log"
Write-Output ''
Write-Output "Run one now:   Start-ScheduledTask -TaskName '$TaskName'"
Write-Output 'Remove it:     .\scripts\register_bench_task.ps1 -Unregister'
