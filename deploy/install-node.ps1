<#
.SYNOPSIS
    Provisions an agent node on a Windows VM: Python, venv, dependencies and node.yaml.

.DESCRIPTION
    Run once per VM, from an elevated PowerShell session. Copy the repository to the
    machine first, then point -SourcePath at the folder containing requirements.txt.

    The bootstrap token is read from the AGENT_BOOTSTRAP_TOKEN environment variable so
    it never appears in a command line or scheduled-task definition.

.EXAMPLE
    $env:AGENT_BOOTSTRAP_TOKEN = "<token>"
    .\install-node.ps1 -AgentId scout-vm-03 `
        -OrchestratorUrl https://xxxx-8000.euw.devtunnels.ms `
        -FoundryEndpoint https://acct.services.ai.azure.com/api/projects/proj `
        -SourcePath C:\Users\me\Desktop\hecaton
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$AgentId,
    [Parameter(Mandatory)][string]$OrchestratorUrl,
    [Parameter(Mandatory)][string]$FoundryEndpoint,
    [Parameter(Mandatory)][string]$SourcePath,
    [string]$FoundryAgent = "john-agent",
    [ValidateSet("scout", "clawdbot", "builtin")][string]$Framework = "scout",
    [string]$Region = "swedencentral",
    [int]$BridgePort = 7801,
    [int]$MaxConcurrency = 2,
    [string]$Root = "C:\hecaton"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path (Join-Path $SourcePath "requirements.txt"))) {
    throw "SourcePath '$SourcePath' does not contain requirements.txt"
}

Write-Host "== Creating directories under $Root"
$app = Join-Path $Root "app"
foreach ($dir in @($Root, "$Root\workspace", "$Root\logs", "$Root\data")) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Write-Host "== Copying application source"
if (Test-Path $app) { Remove-Item $app -Recurse -Force }
New-Item -ItemType Directory -Force -Path $app | Out-Null
Get-ChildItem $SourcePath -Force |
    Where-Object { $_.Name -notin @(".venv", ".git", ".pytest_cache", "data") } |
    Copy-Item -Destination $app -Recurse -Force
Remove-Item "$app\*.mp4", "$app\.env", "$app\.dev-api-key" -Force -ErrorAction SilentlyContinue

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "== Installing Python 3.11"
    $installer = Join-Path $env:TEMP "python-3.11.9-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $installer
    Start-Process $installer -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1" -Wait
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine")
}

Write-Host "== Creating virtual environment"
$venv = Join-Path $Root ".venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    py -3.11 -m venv $venv
}
$python = Join-Path $venv "Scripts\python.exe"

Write-Host "== Installing dependencies"
& $python -m pip install --upgrade pip --quiet
& $python -m pip install -r "$app\requirements.txt" --quiet
& $python -m pip install -r "$app\requirements-foundry.txt" --quiet
& $python -c "import azure.ai.projects, azure.identity; print('foundry sdk ok')"

Write-Host "== Setting machine-level environment"
[Environment]::SetEnvironmentVariable("FOUNDRY_PROJECT_ENDPOINT", $FoundryEndpoint, "Machine")
[Environment]::SetEnvironmentVariable("FOUNDRY_DEFAULT_AGENT", $FoundryAgent, "Machine")
# DefaultAzureCredential reaches IMDS on a link-local address; a proxy must not intercept it.
[Environment]::SetEnvironmentVariable("NO_PROXY", "169.254.169.254,localhost,127.0.0.1", "Machine")
$env:FOUNDRY_PROJECT_ENDPOINT = $FoundryEndpoint

Write-Host "== Verifying managed identity against Foundry"
& $python -c @"
import os
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
with DefaultAzureCredential() as cred, AIProjectClient(endpoint=os.environ['FOUNDRY_PROJECT_ENDPOINT'], credential=cred) as c:
    names = [d.name for d in c.deployments.list()]
print('deployments:', names or 'NONE - deploy a model in Foundry')
"@

Write-Host "== Writing node.yaml"
$scoutEnabled = if ($Framework -eq "scout") { "true" } else { "false" }
$clawdbotEnabled = if ($Framework -eq "clawdbot") { "true" } else { "false" }
$bridgeEndpoint = "http://127.0.0.1:$BridgePort"

$config = @"
orchestrator_url: $OrchestratorUrl
verify_tls: true

agent_id: $AgentId
name: $AgentId
framework: $Framework
platform: azure_vm
max_concurrency: $MaxConcurrency

labels:
  region: $Region
  environment: demo
  os: windows

foundry:
  enabled: true
  max_output_tokens: 2048

frameworks:
  scout:
    enabled: $scoutEnabled
    endpoint: $bridgeEndpoint
    verify_tls: false
  clawdbot:
    enabled: $clawdbotEnabled
    endpoint: $bridgeEndpoint
    verify_tls: false

builtin:
  enabled: true
  workspace_root: $Root\workspace
  allow_http: true
  # Deny-by-default allow-list: each key maps to a fixed argv, never a shell string.
  allowed_commands:
    disk_report:
      - powershell
      - -NoProfile
      - -NonInteractive
      - -Command
      - Get-Volume | Select-Object DriveLetter,SizeRemaining,Size | ConvertTo-Json
    host_info:
      - powershell
      - -NoProfile
      - -NonInteractive
      - -Command
      - Get-ComputerInfo | Select-Object CsName,OsName,OsVersion | ConvertTo-Json
"@
Set-Content -Path (Join-Path $Root "node.yaml") -Value $config -Encoding UTF8

Write-Host ""
Write-Host "== Done. Node provisioned at $Root" -ForegroundColor Green
Write-Host "   Register services:  .\register-services.ps1 -Root $Root -BridgePort $BridgePort"
Write-Host "   Or run in foreground:"
Write-Host "     `$env:AGENT_BOOTSTRAP_TOKEN = '<token>'"
Write-Host "     & '$python' -m uvicorn agent_bridge.foundry_bridge:app --host 127.0.0.1 --port $BridgePort"
Write-Host "     & '$python' -m agent_node.runner --config $Root\node.yaml"
