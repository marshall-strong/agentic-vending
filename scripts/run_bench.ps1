<#
.SYNOPSIS
    Run the standing benchmark once: right-click > "Run with PowerShell", or let the daily task run it.

.DESCRIPTION
    Loads the team Anthropic key into THIS process only (never saved, never printed), shows what will
    run and today's team test cap, asks you to type yes, runs `python -m vending.bench run`, removes
    the key again, then prints the report.

    The key comes from ~\.vending\anthropic-key, a file only your Windows account can read (the
    README's "The Anthropic API key is for the agent only" says how to make one). Without that file,
    your VENDING_ANTHROPIC_API_KEY user environment variable is used, if you set one.

    Any other arguments go to `python -m vending.bench run` as they are. Quote a list, or
    PowerShell splits it at the commas:

        .\scripts\run_bench.ps1 --note "prompt: name the threshold"
        .\scripts\run_bench.ps1 --configs "haiku@medium,opus@low" --repeats 2

    See docs\benchmark.md.

.PARAMETER Yes
    Don't ask: run straight after the dry run. For the scheduled task
    (scripts\register_bench_task.ps1), which has no one to type yes. The budget and the team's daily
    test cap still apply. Output, errors included, also goes to data\logs\bench-<date>.log.
#>

param([switch]$Yes)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$KeyFile = Join-Path $HOME '.vending\anthropic-key'
$Extra = @($args)
$Logging = $false
$go = $false

Set-Location $RepoRoot
if ($Yes) {
    $LogDir = Join-Path $RepoRoot 'data\logs'
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    Start-Transcript -Path (Join-Path $LogDir ("bench-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))) -Append | Out-Null
    $Logging = $true
}

# Runs python and returns its exit code. Its output goes through Out-Host, or Windows PowerShell's
# transcript misses a native program's output, and its stderr is merged in, or the log would miss
# tracebacks and refusals. Merged stderr lines arrive as error records, which 'Stop' would turn into
# a terminating error at the first one, so this function runs with 'Continue'.
function Invoke-Python {
    $ErrorActionPreference = 'Continue'
    & $Python @args 2>&1 | ForEach-Object { "$_" } | Out-Host
    return $LASTEXITCODE
}

$code = 1
try {
    if (-not (Test-Path $Python)) {
        throw "No Python at $Python. Create the venv: py -3 -m venv .venv, then .venv\Scripts\python.exe -m pip install -r requirements.txt"
    }
    if (Test-Path $KeyFile) {
        $key = "$(Get-Content -Raw $KeyFile)".Trim()
        if (-not $key) { throw "$KeyFile is empty. Nothing was spent." }
    } else {
        # The user variable the README also describes. A scheduled task that runs as you sees it too.
        $key = "$([Environment]::GetEnvironmentVariable('VENDING_ANTHROPIC_API_KEY', 'User'))".Trim()
        if (-not $key) {
            throw ("No key: no $KeyFile and no VENDING_ANTHROPIC_API_KEY user variable. See the README, " +
                   "'The Anthropic API key is for the agent only'. Nothing was spent.")
        }
    }

    try {
        # The vending name only: an ANTHROPIC_* variable would be read by Claude Code and bill the team key.
        $env:VENDING_ANTHROPIC_API_KEY = $key
        $key = $null

        Write-Host "`n=== The plan (nothing is sent yet) ===`n" -ForegroundColor Cyan
        if ((Invoke-Python -m vending.bench run --dry-run @Extra) -ne 0) { throw "The dry run failed; nothing was spent." }

        $go = $true
        if (-not $Yes) {
            $answer = Read-Host "`nRun the benchmark now? Type yes to go"
            $go = ($answer -eq 'yes')
        }

        if ($go) {
            Write-Host "`n=== Running the benchmark (about 5-15 minutes) ===`n" -ForegroundColor Cyan
            $code = Invoke-Python -m vending.bench run @Extra
        } else {
            Write-Host 'Stopped. Nothing was spent.'
            $code = 0
        }
    }
    finally {
        Remove-Item Env:VENDING_ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
    }

    if ($go -and $code -eq 0) {
        Write-Host "`n=== The last five runs ===`n" -ForegroundColor Cyan
        Invoke-Python -m vending.bench report --history 5 | Out-Null
    } elseif ($go) {
        Write-Host "`nThe benchmark stopped with exit code $code. See above; python -m vending.bench list shows the run." -ForegroundColor Yellow
    }
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $code = 1
}
finally {
    if ($Logging) { Stop-Transcript | Out-Null }
}

if (-not $Yes) { Read-Host 'Press Enter to close' | Out-Null }
exit $code
