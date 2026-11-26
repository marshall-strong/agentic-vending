<#
.SYNOPSIS
Swap the model key on the Azure Container App, outside an agent run, and check the worker has it (#262).

.DESCRIPTION
    .\scripts\swap_model_key.ps1 -KeyFile C:\path\to\new-key.txt

The key file holds the new key alone, on one line. The key is never on a command line, never typed
at a prompt and never printed. In order, the script:

1. Checks the team database (DATABASE_URL in .env, parsed as the app parses it) and stops while an
   agent run holds the run lease or a scheduled time says running: a restart would cut the run off,
   and a scheduled time is never run again (docs/agent.md).
2. Checks the new key with one free count_tokens call (never a paid request). It stops if the key
   has no credit or the API refuses it.
3. Checks the app's VENDING_ANTHROPIC_API_KEY reads the secret it is about to set.
4. Sets the secret from a temporary file (name=@file, as deploy_container_app.ps1 does), then
   deletes the file.
5. Restarts the latest revision (a revision reads its secrets only when it starts) and waits until
   the revision is running and healthy and /health answers from the restarted worker.
6. Checks /health says the worker has the model key (model_key: true).

Steps 1 and 2 are scripts/check_model_key.py. Exit codes: 0 swapped and checked; 1 an az step
failed, or the restarted worker wasn't ready in time; 2 to 7 stopped before any change in Azure
(see scripts/check_model_key.py; 8: the app doesn't read the secret); 9 swapped and restarted, but
/health can't show the model key (scheduled runs are off on this revision) or says it is missing.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$KeyFile,
    [string]$Subscription = "6eb895dc-71d3-448b-9905-9745666c1769",
    [string]$ResourceGroup = "data_systems_resource_group",
    [string]$App = "ai-group-2-vend",
    # The secret the container's VENDING_ANTHROPIC_API_KEY reads (deploy_container_app.ps1, $OptionalSecrets).
    [string]$SecretName = "vending-anthropic-api-key",
    [string]$EnvFile,
    [int]$ReadySeconds = 600
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
# Not a default in param(): Windows PowerShell 5.1 leaves $PSScriptRoot empty there under powershell -File.
if (-not $EnvFile) { $EnvFile = Join-Path $root ".env" }
$KeyEnv = "VENDING_ANTHROPIC_API_KEY"

if (-not (Test-Path -LiteralPath $KeyFile -PathType Leaf)) { throw "No key file at $KeyFile" }
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

# Runs a native command with stderr not turned into errors (Windows PowerShell 5.1 makes each stderr line an error
# record, which "Stop" makes terminating); the exit code decides, as in PowerShell 7.
function Invoke-Native([scriptblock]$Command) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $saved }
}

$dir = Join-Path ([IO.Path]::GetTempPath()) "agentic-vending-key-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $dir | Out-Null
try {
    $keyCopy = Join-Path $dir $SecretName
    Write-Host "Checking that no agent run is going, and that the new key has credit (a free count_tokens call)..."
    Invoke-Native { & $python (Join-Path $PSScriptRoot "check_model_key.py") $KeyFile $EnvFile $keyCopy }
    $checked = $LASTEXITCODE
    if ($checked -ne 0) {
        Write-Host "Stopped before any change in Azure."
        exit $checked
    }

    az account set --subscription $Subscription
    if ($LASTEXITCODE -ne 0) { throw "az account set failed; run az login --tenant golisanoinstitute.org first" }

    # The secret only matters if the container reads it: a first deploy without the key gave it no such variable.
    # No space or pipe in the query: az is a .cmd file, and cmd.exe reads its arguments again.
    $reads = @(az containerapp show --name $App --resource-group $ResourceGroup `
            --query "properties.template.containers[0].env[?name=='$KeyEnv'].secretRef" --output tsv)
    if ($LASTEXITCODE -ne 0) { throw "az containerapp show failed" }
    if (-not (@($reads | ForEach-Object { "$_".Trim() }) -contains $SecretName)) {
        Write-Host "STOP: $App's $KeyEnv doesn't read the secret $SecretName, so swapping it would change nothing. Put the key in .env and deploy with -SetSecrets (docs/deploy.md) instead. Nothing was changed."
        exit 8
    }

    $fqdn = az containerapp show --name $App --resource-group $ResourceGroup --query "properties.configuration.ingress.fqdn" --output tsv
    if ($LASTEXITCODE -ne 0) { throw "az containerapp show failed" }
    $health = "https://$fqdn/health"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $startedBefore = $null
    try { $startedBefore = (Invoke-RestMethod -Uri $health -TimeoutSec 20).started_at } catch { }

    Write-Host "Setting the secret $SecretName on $App (az reads the value from a file; it is on no command line)..."
    az containerapp secret set --name $App --resource-group $ResourceGroup --secrets "$SecretName=@$keyCopy" --output none
    if ($LASTEXITCODE -ne 0) { throw "az containerapp secret set failed; the old key is still in place" }
} finally {
    Remove-Item -Recurse -Force $dir
}

# A revision reads its secrets only when it starts, and a changed secret makes no new revision.
$revision = az containerapp show --name $App --resource-group $ResourceGroup --query "properties.latestRevisionName" --output tsv
if ($LASTEXITCODE -ne 0) { throw "az containerapp show failed" }
Write-Host "Restarting $revision so the worker reads the new key..."
az containerapp revision restart --name $App --resource-group $ResourceGroup --revision $revision --output none
if ($LASTEXITCODE -ne 0) { throw "az containerapp revision restart failed; the secret is set, but $revision still has the old key. Run: az containerapp revision restart --name $App --resource-group $ResourceGroup --revision $revision" }

Write-Host "Waiting up to $ReadySeconds s for $revision to run again and for /health to answer from the restarted worker..."
$deadline = (Get-Date).AddSeconds($ReadySeconds)
$shown = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    $state = Invoke-Native { az containerapp revision show --name $App --resource-group $ResourceGroup `
            --revision $revision --query "[properties.runningState,properties.healthState]" --output tsv 2>$null }
    if ($LASTEXITCODE -ne 0) { continue }
    $state = @("$state" -split "\s+" | Where-Object { $_ })
    if (-not (($state -contains "Running" -or $state -contains "RunningAtMaxScale") -and $state -contains "Healthy")) { continue }
    try { $answer = Invoke-RestMethod -Uri $health -TimeoutSec 20 } catch { continue }
    # A restarted worker has a new started_at; before the restart lands, the old worker still answers.
    if ($startedBefore -and $answer.started_at -eq $startedBefore) { continue }
    $shown = $answer
    break
}
if ($null -eq $shown) {
    Write-Host "The secret is set and $revision was restarted, but it wasn't running and answering /health within $ReadySeconds s. Check: az containerapp revision show --name $App --resource-group $ResourceGroup --revision $revision"
    exit 1
}
Write-Host "$revision is running and healthy; the worker started at $($shown.started_at)."

$schedule = $shown.agent_schedule
if ($null -eq $schedule -or $null -eq $schedule.model_key) {
    Write-Host "Swapped and restarted, but /health can't show the model key: scheduled agent runs are off on this revision (AGENT_SCHEDULE_MODE unset or off). Check after they are on: $health"
    exit 9
}
if ($schedule.model_key -ne $true) {
    Write-Host "Swapped and restarted, but /health says the worker has no model key. Check $App's $KeyEnv and the secret $SecretName."
    exit 9
}
Write-Host "Done: /health says the worker has the model key (model_key: true). The key was not printed."
Write-Host "The next scheduled time checks the key's credit before it claims; python -m vending.agent_schedule shows the rows."
exit 0
