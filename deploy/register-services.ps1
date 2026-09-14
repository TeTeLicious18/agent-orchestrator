<#
.SYNOPSIS
    Registers the Foundry bridge and the agent node as auto-starting Windows services.

.DESCRIPTION
    Uses NSSM, downloaded on first run. Both services run as LocalSystem because
    DefaultAzureCredential needs access to the IMDS endpoint for managed identity.

    The bootstrap token is read from the AGENT_BOOTSTRAP_TOKEN environment variable of
    the calling session and stored in the service definition. Rotate it after enrolment:
    once registered, the node authenticates with its own HMAC secret instead.

.EXAMPLE
    $env:AGENT_BOOTSTRAP_TOKEN = "<token>"
    .\register-services.ps1
#>
[CmdletBinding()]
param(
    [string]$Root = "C:\hecaton",
    [int]$BridgePort = 7801,
    [string]$ServicePrefix = "Hecaton"
)

$ErrorActionPreference = "Stop"

$token = $env:AGENT_BOOTSTRAP_TOKEN
if (-not $token) {
    throw "Set AGENT_BOOTSTRAP_TOKEN in this session before running this script."
}

$python = Join-Path $Root ".venv\Scripts\python.exe"
$app = Join-Path $Root "app"
$logs = Join-Path $Root "logs"
foreach ($path in @($python, $app, (Join-Path $Root "node.yaml"))) {
    if (-not (Test-Path $path)) { throw "Missing $path - run install-node.ps1 first." }
}
New-Item -ItemType Directory -Force -Path $logs | Out-Null

$nssm = Join-Path $Root "tools\nssm.exe"
if (-not (Test-Path $nssm)) {
    Write-Host "== Downloading NSSM"
    $zip = Join-Path $env:TEMP "nssm.zip"
    Invoke-WebRequest "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip
    Expand-Archive $zip -DestinationPath (Join-Path $Root "tools") -Force
    Copy-Item (Join-Path $Root "tools\nssm-2.24\win64\nssm.exe") $nssm -Force
}

function Register-HecatonService {
    param(
        [string]$Name,
        [string]$Arguments,
        [string[]]$Environment,
        [string]$DependsOn
    )

    if (Get-Service $Name -ErrorAction SilentlyContinue) {
        Write-Host "== Removing existing service $Name"
        & $nssm stop $Name confirm | Out-Null
        & $nssm remove $Name confirm | Out-Null
        Start-Sleep -Seconds 2
    }

    Write-Host "== Registering $Name"
    & $nssm install $Name $python $Arguments | Out-Null
    & $nssm set $Name AppDirectory $app | Out-Null
    & $nssm set $Name AppStdout (Join-Path $logs "$Name.log") | Out-Null
    & $nssm set $Name AppStderr (Join-Path $logs "$Name.err.log") | Out-Null
    & $nssm set $Name AppRotateFiles 1 | Out-Null
    & $nssm set $Name AppRotateBytes 10485760 | Out-Null
    & $nssm set $Name ObjectName LocalSystem | Out-Null
    & $nssm set $Name Start SERVICE_AUTO_START | Out-Null
    if ($Environment) { & $nssm set $Name AppEnvironmentExtra $Environment | Out-Null }
    if ($DependsOn) { & $nssm set $Name DependOnService $DependsOn | Out-Null }
}

$bridgeService = "${ServicePrefix}Bridge"
$agentService = "${ServicePrefix}Agent"

Register-HecatonService -Name $bridgeService `
    -Arguments "-m uvicorn agent_bridge.foundry_bridge:app --host 127.0.0.1 --port $BridgePort"

Register-HecatonService -Name $agentService `
    -Arguments "-m agent_node.runner --config $Root\node.yaml" `
    -Environment @("AGENT_BOOTSTRAP_TOKEN=$token") `
    -DependsOn $bridgeService

Start-Service $bridgeService
Start-Sleep -Seconds 3
Start-Service $agentService

Get-Service $bridgeService, $agentService | Format-Table Name, Status, StartType

Write-Host ""
Write-Host "== Services running. Follow the logs with:" -ForegroundColor Green
Write-Host "   Get-Content $logs\$agentService.log -Wait"
