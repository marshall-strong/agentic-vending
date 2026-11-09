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

The image is tagged with the commit, so it builds only from a clean checkout of main that
matches origin/main (it fetches to check), and -SkipBuild without -Tag checks the same; -Force
deploys anyway, and a tree with uncommitted changes then gets a "-dirty" tag.

Before anything in Azure changes, it checks the image can be pulled anonymously, as the
Container App pulls it. The first push of the package fails that check, because ghcr.io
packages start private: make it public, then rerun with -Tag <tag> -SkipBuild.

Secrets are read from .env by scripts/write_secret_files.py, which parses it exactly as the
app does, into one file each in a temporary folder that is deleted afterwards. az reads each
value from its file (name=@path), so no value is ever on a command line, where other processes
or az's own command log could see it. Nothing here migrates the database; run
python -m vending.db migrate yourself when a merge adds a migration.
#>
[CmdletBinding()]
param(
    [string]$Tag,
    [string]$Image = "ghcr.io/marshall-strong/agentic-vending",
    [string]$Subscription = "6eb895dc-71d3-448b-9905-9745666c1769",
    [string]$ResourceGroup = "data_systems_resource_group",
    [string]$App = "ai-group-2-vend",
    [int]$MinReplicas = 1,
    [int]$MaxReplicas = 1,
    [switch]$SkipBuild,
    [switch]$SetSecrets,
    [switch]$Force,
    [switch]$SkipPullCheck,
    [string]$EnvFile = (Join-Path $PSScriptRoot "..\.env")
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
# The image runs as a normal user, which can't listen below 1024.
$Port = 8080
# .env setting -> Container App secret name.
$Secrets = [ordered]@{ "DATABASE_URL" = "database-url"; "DEX_API_KEY" = "dex-api-key"; "TS_AUTHKEY" = "ts-authkey"; "ANTHROPIC_API_KEY" = "anthropic-api-key" }

# Whenever the tag comes from HEAD (a build, or -SkipBuild without -Tag), HEAD must be
# origin/main exactly, so the tag names a commit everyone can see on main.
if (-not ($SkipBuild -and $Tag)) {
    $branch = git -C $root rev-parse --abbrev-ref HEAD
    $dirty = git -C $root status --porcelain
    if (($branch -ne "main" -or $dirty) -and -not $Force) {
        $what = if ($dirty) { "'$branch' with uncommitted changes" } else { "'$branch'" }
        throw "Refusing to deploy from $what; deploy a clean checkout of main, or pass -Force"
    }
    if (-not $Force) {
        git -C $root fetch --quiet origin main
        if ($LASTEXITCODE -ne 0) { throw "git fetch origin main failed, so HEAD can't be compared with origin/main; pass -Force to deploy anyway" }
        $head = git -C $root rev-parse HEAD
        $upstream = git -C $root rev-parse origin/main
        if ($head -ne $upstream) { throw "HEAD ($($head.Substring(0, 7))) is not origin/main ($($upstream.Substring(0, 7))); pull or push main first, or pass -Force" }
    }
}
if (-not $Tag) {
    $Tag = git -C $root rev-parse --short HEAD
    if (-not $SkipBuild -and $dirty) { $Tag = "$Tag-dirty" }
}
$full = "${Image}:${Tag}"
Write-Host "Image: $full"

if (-not $SkipBuild) {
    Write-Host "Building..."
    docker build -t $full $root
    if ($LASTEXITCODE -ne 0) { throw "docker build failed" }
    Write-Host "Pushing..."
    docker push $full
    if ($LASTEXITCODE -ne 0) { throw "docker push failed; run: docker login ghcr.io -u <github user> (password: a token with write:packages)" }
}

# The Container App pulls without registry credentials, so check the image is public before
# anything in Azure changes: a new package on ghcr.io starts private. An empty config folder
# makes docker ask anonymously, as Azure will.
if (-not $SkipPullCheck) {
    $anonymous = Join-Path ([IO.Path]::GetTempPath()) "agentic-vending-docker-$([guid]::NewGuid())"
    New-Item -ItemType Directory -Path $anonymous | Out-Null
    try {
        docker --config $anonymous manifest inspect $full *> $null
        $public = $LASTEXITCODE -eq 0
    } finally {
        Remove-Item -Recurse -Force $anonymous
    }
    if (-not $public) {
        $package = $Image.Split("/")[-1]
        throw "Stopping before any change in Azure: $full can't be pulled anonymously, so the Container App couldn't pull it either. If this was the first push, make the package public (github.com -> $($Image.Split("/")[1]) -> Packages -> $package -> Package settings -> Change visibility), then rerun with -Tag $Tag -SkipBuild. Otherwise check the tag was pushed. -SkipPullCheck skips this check."
    }
}

az account set --subscription $Subscription
if ($LASTEXITCODE -ne 0) { throw "az account set failed; run az login --tenant golisanoinstitute.org first" }

$dexBase = $null
if ($SetSecrets) {
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { $python = "python" }
    $dir = Join-Path ([IO.Path]::GetTempPath()) "agentic-vending-secrets-$([guid]::NewGuid())"
    New-Item -ItemType Directory -Path $dir | Out-Null
    try {
        $specs = @($Secrets.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) + "DEX_BASE_URL=dex-base-url"
        & $python (Join-Path $PSScriptRoot "write_secret_files.py") $EnvFile $dir @specs
        if ($LASTEXITCODE -ne 0) { throw "could not read the secrets from $EnvFile" }
        $dexBase = [IO.File]::ReadAllText((Join-Path $dir "dex-base-url"))
        if (-not $dexBase.StartsWith("http://")) { throw "DEX_BASE_URL must start with http://: only plain-http traffic goes through Tailscale, so an https bridge URL would be unreachable" }
        Write-Host "Setting secrets from $EnvFile (az reads each value from a file; none is on a command line)..."
        $secretArgs = @($Secrets.Values | ForEach-Object { "$_=@" + (Join-Path $dir $_) })
        az containerapp secret set --name $App --resource-group $ResourceGroup --secrets @secretArgs --output none
        if ($LASTEXITCODE -ne 0) { throw "az containerapp secret set failed" }
    } finally {
        Remove-Item -Recurse -Force $dir
    }
} else {
    # The env vars below point at these secrets, and the update fails if one doesn't exist.
    $have = @(az containerapp secret list --name $App --resource-group $ResourceGroup --query "[].name" --output tsv)
    if ($LASTEXITCODE -ne 0) { throw "az containerapp secret list failed" }
    $missing = @($Secrets.Values | Where-Object { $have -notcontains $_ })
    if ($missing) { throw "$App has no secret $($missing -join ', '); run once with -SetSecrets" }
}

$before = az containerapp show --name $App --resource-group $ResourceGroup --query "properties.latestRevisionName" --output tsv
if ($LASTEXITCODE -ne 0) { throw "az containerapp show failed" }

# Before the image changes, so the new revision's health probes go to the port it listens on.
# Moved back below if the update fails, so the running revision keeps the port it listens on.
$targetPort = az containerapp ingress show --name $App --resource-group $ResourceGroup --query "targetPort" --output tsv
if ($LASTEXITCODE -ne 0) { throw "az containerapp ingress show failed" }
if ($targetPort -ne "$Port") {
    Write-Host "Pointing ingress at port $Port (was $targetPort)..."
    az containerapp ingress update --name $App --resource-group $ResourceGroup --target-port $Port --output none
    if ($LASTEXITCODE -ne 0) { throw "az containerapp ingress update failed" }
}

Write-Host "Rolling $App to $full with $MinReplicas-$MaxReplicas replicas..."
$envVars = @($Secrets.GetEnumerator() | ForEach-Object { "$($_.Key)=secretref:$($_.Value)" }) + @("VENDING_TOOLS=built", "TS_HOSTNAME=agentic-vending-azure", "PORT=$Port")
if ($dexBase) { $envVars += "DEX_BASE_URL=$dexBase" }
az containerapp update --name $App --resource-group $ResourceGroup --image $full --min-replicas $MinReplicas --max-replicas $MaxReplicas --set-env-vars @envVars --output none
if ($LASTEXITCODE -ne 0) {
    if ($targetPort -ne "$Port") {
        Write-Host "Pointing ingress back at port $targetPort..."
        az containerapp ingress update --name $App --resource-group $ResourceGroup --target-port $targetPort --output none
        if ($LASTEXITCODE -ne 0) { throw "az containerapp update failed, and so did moving ingress back to port $targetPort; run: az containerapp ingress update --name $App --resource-group $ResourceGroup --target-port $targetPort" }
    }
    throw "az containerapp update failed"
}

# A revision reads its secrets when it starts, and changed secrets alone make no new revision.
$after = az containerapp show --name $App --resource-group $ResourceGroup --query "properties.latestRevisionName" --output tsv
if ($LASTEXITCODE -ne 0) { throw "az containerapp show failed" }
if ($SetSecrets -and $after -eq $before) {
    Write-Host "No new revision, so restarting $after to load the new secrets..."
    az containerapp revision restart --name $App --resource-group $ResourceGroup --revision $after --output none
    if ($LASTEXITCODE -ne 0) { throw "az containerapp revision restart failed; the secrets are set, but $after still has the old ones" }
}

$fqdn = az containerapp show --name $App --resource-group $ResourceGroup --query "properties.configuration.ingress.fqdn" --output tsv
Write-Host "Done. Health: https://$fqdn/health"
Write-Host "Logs:   az containerapp logs show --name $App --resource-group $ResourceGroup --follow"
Write-Host "Revisions: az containerapp revision list --name $App --resource-group $ResourceGroup --output table"
