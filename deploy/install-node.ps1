<#
.SYNOPSIS
    Provisions an agent node on a Windows machine, from Git clone to running configuration.

.DESCRIPTION
    Installs Git and Python if missing, clones the repository, creates a virtual environment,
    installs the requested capability extras, verifies the machine's managed identity against
    Foundry, and writes node.yaml and node.env.

    The bootstrap token is read from the AGENT_BOOTSTRAP_TOKEN environment variable so it
    never appears in a command line or in shell history.

.EXAMPLE
    $env:AGENT_BOOTSTRAP_TOKEN = "<token>"
    .\install-node.ps1 -AgentId vm1-bob `
        -OrchestratorUrl https://xxxx-8000.euw.devtunnels.ms `
        -FoundryEndpoint https://acct.services.ai.azure.com/api/projects/proj `
        -FoundryAgent bob-agent -Capabilities all
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$AgentId,
    [Parameter(Mandatory)][string]$OrchestratorUrl,
    [Parameter(Mandatory)][string]$FoundryEndpoint,
    [string]$FoundryAgent = "",
    [ValidateSet("core", "research", "all")][string]$Capabilities = "all",
    [string]$Repository = "https://github.com/TeTeLicious18/agent-orchestrator.git",
    [string]$Root = "C:\hecaton",
    [string]$Region = "swedencentral",
    [int]$BridgePort = 7801,
    [int]$MaxConcurrency = 2
)

$ErrorActionPreference = "Stop"

$token = $env:AGENT_BOOTSTRAP_TOKEN
if (-not $token) { throw "Set AGENT_BOOTSTRAP_TOKEN in this session first." }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "== Installing Git"
    winget install --id Git.Git --accept-package-agreements --accept-source-agreements --silent | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "== Installing Python 3.11"
    $installer = Join-Path $env:TEMP "python-3.11.9-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $installer
    Start-Process $installer -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1" -Wait
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine")
}

# --------------------------------------------------------------- directories
$app = Join-Path $Root "app"
$workspace = Join-Path $env:USERPROFILE "Desktop\agent-output"
foreach ($dir in @($Root, "$Root\workspace", "$Root\logs", $workspace)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Write-Host "== Fetching the application"
if (Test-Path (Join-Path $app ".git")) {
    git -C $app pull --ff-only
} else {
    if (Test-Path $app) { Remove-Item $app -Recurse -Force }
    git clone --depth 1 $Repository $app
}

# ------------------------------------------------------------------- python
Write-Host "== Creating the virtual environment"
$venv = Join-Path $Root ".venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) { py -3.11 -m venv $venv }
$python = Join-Path $venv "Scripts\python.exe"

$extras = switch ($Capabilities) {
    "core" { @("requirements.txt", "requirements-foundry.txt") }
    "research" { @("requirements.txt", "requirements-foundry.txt", "requirements-browser.txt") }
    "all" { @("requirements.txt", "requirements-foundry.txt", "requirements-browser.txt",
              "requirements-desktop.txt", "requirements-office.txt") }
}

Write-Host "== Installing dependencies ($Capabilities)"
& $python -m pip install --upgrade pip --quiet
foreach ($file in $extras) { & $python -m pip install -r (Join-Path $app $file) --quiet }

# -------------------------------------------------------------- environment
[Environment]::SetEnvironmentVariable("FOUNDRY_PROJECT_ENDPOINT", $FoundryEndpoint, "Machine")
# DefaultAzureCredential reaches IMDS on a link-local address; a proxy must not intercept it.
[Environment]::SetEnvironmentVariable("NO_PROXY", "169.254.169.254,localhost,127.0.0.1", "Machine")
$env:FOUNDRY_PROJECT_ENDPOINT = $FoundryEndpoint
$env:NO_PROXY = "169.254.169.254,localhost,127.0.0.1"

Write-Host "== Verifying managed identity against Foundry"
& $python -c @"
import os
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
with DefaultAzureCredential() as cred, AIProjectClient(endpoint=os.environ['FOUNDRY_PROJECT_ENDPOINT'], credential=cred) as c:
    names = [d.name for d in c.deployments.list()]
print('deployments:', names or 'NONE - assign Cognitive Services User to this VM, or deploy a model')
"@

# ------------------------------------------------------------------ config
$full = $Capabilities -eq "all"
$browserEnabled = if ($Capabilities -in @("research", "all")) { "true" } else { "false" }
$desktopEnabled = if ($full) { "true" } else { "false" }
$officeEnabled = if ($full) { "true" } else { "false" }
$bridgeEnabled = if ($FoundryAgent) { "true" } else { "false" }

$apps = @'
      notepad: ["notepad.exe"]
      wordpad: ["write.exe"]
      paint: ["mspaint.exe"]
      calculator: ["calc.exe"]
      explorer: ["explorer.exe"]
      edge: ["msedge.exe"]
      excel: ["excel.exe"]
'@
$vscode = (Get-ChildItem "$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe",
    "C:\Program Files\Microsoft VS Code\Code.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1).FullName
if ($vscode) { $apps += "      vscode: [`"$($vscode -replace '\\', '\\')`"]`n" }

$pythonYaml = $python -replace '\\', '\\'

$config = @"
orchestrator_url: $OrchestratorUrl
verify_tls: true

agent_id: $AgentId
name: $AgentId
framework: builtin
platform: azure_vm
max_concurrency: $MaxConcurrency

labels:
  region: $Region
  environment: demo
  os: windows

foundry:
  enabled: true
  max_output_tokens: 2048
  request_timeout: 120

frameworks:
  scout:
    enabled: $bridgeEnabled
    endpoint: http://127.0.0.1:$BridgePort
    verify_tls: false
  clawdbot:
    enabled: false

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

autonomous:
  enabled: true
  allow_desktop: $desktopEnabled
  max_steps: 20
  workspace_root: $workspace
  browser:
    enabled: $browserEnabled
    headless: false
    channel: msedge
    allowed_domains: []
  excel:
    enabled: $officeEnabled
  code_execution:
    enabled: $officeEnabled
    timeout_seconds: 60
    runtimes:
      python: ["$pythonYaml"]
      powershell: ["powershell", "-NoProfile", "-NonInteractive", "-File"]
  desktop_control:
    enabled: $desktopEnabled
    type_interval: 0.02
    apps:
$apps
"@
Set-Content -Path (Join-Path $Root "node.yaml") -Value $config -Encoding UTF8

@"
ORCHESTRATOR_URL=$OrchestratorUrl
AGENT_BOOTSTRAP_TOKEN=$token
FOUNDRY_PROJECT_ENDPOINT=$FoundryEndpoint
FOUNDRY_DEFAULT_AGENT=$FoundryAgent
"@ | Set-Content -Path (Join-Path $Root "node.env") -Encoding UTF8

& $python -c "import yaml; yaml.safe_load(open(r'$Root\node.yaml')); print('node.yaml is valid')"

Write-Host ""
Write-Host "================ NODE PROVISIONED ================" -ForegroundColor Green
Write-Host " agent_id     : $AgentId"
Write-Host " capabilities : $Capabilities"
Write-Host " foundry agent: $(if ($FoundryAgent) { $FoundryAgent } else { 'none - bridge disabled' })"
Write-Host " workspace    : $workspace"
Write-Host ""
Write-Host " Start it with:" -ForegroundColor Cyan
Write-Host "   $app\deploy\start-vm.ps1"
Write-Host "==================================================" -ForegroundColor Green
