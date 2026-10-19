# ---------------------------------------------------------------------------
# Wrapper that Task Scheduler runs. Not meant to be run by hand -- if you want
# a snapshot right now, run this instead:
#
#     python scripts\poll_snapshot.py
#
# Two jobs:
#
#  1. Give the Python script a predictable environment. The scheduler starts
#     tasks from C:\Windows\System32 with a different PATH from your shell, so
#     nothing here may assume the working directory or rely on a venv.
#
#  2. Leave a trace that the task actually fired. `data\logs\poll.log` is
#     written by the Python script, so it can only tell us about runs that got
#     as far as starting Python. This wrapper writes one line to
#     `data\logs\scheduler.log` every single time it is invoked, before and
#     after. If scheduler.log has entries that poll.log does not, Python is
#     failing to launch. If neither has entries, the task is not firing at all.
#     That difference is the whole reason this file exists.
# ---------------------------------------------------------------------------

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot 'data\logs'
$SchedulerLog = Join-Path $LogDir 'scheduler.log'

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Write-SchedulerLog([string]$Message) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $line = "$stamp $Message"
    Write-Output $line
    Add-Content -Path $SchedulerLog -Value $line -Encoding utf8
}

# Find an interpreter. `py` is the Windows Python launcher and is the most
# reliable option under the scheduler; plain `python` is the fallback.
$Interpreter = $null
$InterpreterArgs = @()
if (Get-Command 'py' -ErrorAction SilentlyContinue) {
    $Interpreter = 'py'
    $InterpreterArgs = @('-3')
} elseif (Get-Command 'python' -ErrorAction SilentlyContinue) {
    $Interpreter = 'python'
}

if (-not $Interpreter) {
    Write-SchedulerLog 'level=ERROR event=no_python detail="neither py nor python is on PATH for this task''s account" exit=9'
    exit 9
}

$Script = Join-Path $PSScriptRoot 'poll_snapshot.py'
Write-SchedulerLog "level=INFO event=task_started script=$Script interpreter=$Interpreter"

try {
    & $Interpreter @InterpreterArgs $Script @args
    $code = $LASTEXITCODE
} catch {
    Write-SchedulerLog "level=ERROR event=launch_failed detail=""$($_.Exception.Message)"" exit=9"
    exit 9
}

Write-SchedulerLog "level=INFO event=task_finished exit=$code"
exit $code
