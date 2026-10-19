# ---------------------------------------------------------------------------
# Wrapper that Task Scheduler runs for the daily archive health check.
# Run it by hand with: python scripts\check_archive.py
#
# The report goes to `data\logs\check.log` as well as to the console, so there
# is a dated record of when the archive was last confirmed healthy -- not just
# a pass/fail that vanishes when the console window closes.
# ---------------------------------------------------------------------------

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot 'data\logs'
$CheckLog = Join-Path $LogDir 'check.log'

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$Interpreter = $null
$InterpreterArgs = @()
if (Get-Command 'py' -ErrorAction SilentlyContinue) {
    $Interpreter = 'py'
    $InterpreterArgs = @('-3')
} elseif (Get-Command 'python' -ErrorAction SilentlyContinue) {
    $Interpreter = 'python'
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')

if (-not $Interpreter) {
    $line = "$stamp level=ERROR event=no_python detail=""neither py nor python is on PATH"""
    Write-Output $line
    Add-Content -Path $CheckLog -Value $line -Encoding utf8
    exit 9
}

$Script = Join-Path $PSScriptRoot 'check_archive.py'
$output = & $Interpreter @InterpreterArgs $Script @args 2>&1
$code = $LASTEXITCODE

$verdict = if ($code -eq 0) { 'OK' } else { 'STALE' }
Add-Content -Path $CheckLog -Value "----- $stamp  result=$verdict exit=$code -----" -Encoding utf8
Add-Content -Path $CheckLog -Value $output -Encoding utf8
Write-Output $output

exit $code
