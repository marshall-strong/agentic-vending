<#
.SYNOPSIS
Build the image, push it to GitHub Container Registry, and roll the Azure Container App to it.

.DESCRIPTION
One command per step, so a step can be rerun on its own. Defaults are the resources David
created on 26 Oct 2026; see docs/deploy.md for the one-time setup (az login, docker login
to ghcr.io, making the package public) and for what to check afterwards.

    .\scripts\deploy_container_app.ps1                      # build, push, update to the current commit
    .\scripts\deploy_container_app.ps1 -SetSecrets           # also (re)load secrets from .env
    .\scripts\deploy_container_app.ps1 -Tag v1 -SkipBuild    # roll to an image that is already pushed

Secrets never appear on the command line as plain text beyond this process: they are read from
.env and handed to az as secret values. Nothing here migrates the database; run
python -m vending.db migrate yourself when a merge adds a migration.
#>
[CmdletBinding()]
param(
    [string]$Tag = (git rev-parse --short HEAD),
    [string]$Image = "ghcr.io/marshall-strong/agentic-vending",
    [string]$Subscription = "6eb895dc-71d3-448b-9905-9745666c1769",
    [string]$ResourceGroup = "data_systems_resource_group",
    [string]$App = "ai-group-2-vend",
    [int]$MinReplicas = 1,
    [int]$MaxReplicas = 1,
    [switch]$SkipBuild,
    [switch]$SetSecrets,
    [string]$EnvFile = (Join-Path $PSScriptRoot "..\.env")
)

$ErrorActionPreference = "Stop"
$full = "${Image}:${Tag}"

function Read-DotEnv([string]$path) {
    $values = @{}
    if (-not (Test-Path $path)) { throw "$path does not exist; copy .env.example and fill it in" }
    foreach ($line in Get-Content $path) {
        $trimmed = $line.Trim()
        if ($trimmed -eq "" -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) { continue }
        $key, $value = $trimmed.Split("=", 2)
        $values[$key.Trim()] = $value.Trim().Trim('"').Trim("'")
    }
    return $values
}

Write-Host "Image: $full"

if (-not $SkipBuild) {
    Write-Host "Building..."
    docker build -t $full (Join-Path $PSScriptRoot "..")
    if ($LASTEXITCODE -ne 0) { throw "docker build failed" }
    Write-Host "Pushing..."
    docker push $full
    if ($LASTEXITCODE -ne 0) { throw "docker push failed; run: docker login ghcr.io -u <github user> (password: a token with write:packages)" }
}

az account set --subscription $Subscription
if ($LASTEXITCODE -ne 0) { throw "az account set failed; run az login --tenant golisanoinstitute.org first" }

if ($SetSecrets) {
    $env_values = Read-DotEnv $EnvFile
    foreach ($name in @("DATABASE_URL", "DEX_BASE_URL", "DEX_API_KEY", "TS_AUTHKEY", "ANTHROPIC_API_KEY")) {
        if (-not $env_values.ContainsKey($name) -or $env_values[$name] -eq "") { throw "$name is empty in $EnvFile" }
    }
    Write-Host "Setting secrets from $EnvFile (values are not shown)..."
    az containerapp secret set --name $App --resource-group $ResourceGroup --secrets "database-url=$($env_values['DATABASE_URL'])" "dex-api-key=$($env_values['DEX_API_KEY'])" "ts-authkey=$($env_values['TS_AUTHKEY'])" "anthropic-api-key=$($env_values['ANTHROPIC_API_KEY'])" --output none
    if ($LASTEXITCODE -ne 0) { throw "az containerapp secret set failed" }
    $dexBase = $env_values["DEX_BASE_URL"]
} else {
    $dexBase = $null
}

Write-Host "Rolling $App to $full with $MinReplicas-$MaxReplicas replicas..."
$envVars = @("DATABASE_URL=secretref:database-url", "DEX_API_KEY=secretref:dex-api-key", "TS_AUTHKEY=secretref:ts-authkey", "ANTHROPIC_API_KEY=secretref:anthropic-api-key", "VENDING_TOOLS=built", "TS_HOSTNAME=agentic-vending-azure", "PORT=80")
if ($dexBase) { $envVars += "DEX_BASE_URL=$dexBase" }
az containerapp update --name $App --resource-group $ResourceGroup --image $full --min-replicas $MinReplicas --max-replicas $MaxReplicas --set-env-vars @envVars --output none
if ($LASTEXITCODE -ne 0) { throw "az containerapp update failed" }

$fqdn = az containerapp show --name $App --resource-group $ResourceGroup --query "properties.configuration.ingress.fqdn" --output tsv
Write-Host "Done. Health: https://$fqdn/health"
Write-Host "Logs:   az containerapp logs show --name $App --resource-group $ResourceGroup --follow"
Write-Host "Revisions: az containerapp revision list --name $App --resource-group $ResourceGroup --output table"
