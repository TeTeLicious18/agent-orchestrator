<#
.SYNOPSIS
    One-shot startup for an agent node: loads node.env, starts the Foundry bridge and the runner.

.DESCRIPTION
    Reads C:\hecaton\node.env (written from the block that start-laptop.ps1 prints), keeps
    node.yaml in sync with the current orchestrator URL, then launches the bridge and the
    node in separate windows and verifies each one before moving on.

.EXAMPLE
    .\start-vm.ps1
#>
[CmdletBinding()]
param(
    [string]$Root = "C:\hecaton",
    [int]$BridgePort = 7801
)

$ErrorActionPreference = "Stop"

$envFile = Join-Path $Root "node.env"
$python = Join-Path $Root ".venv\Scripts\python.exe"
$app = Join-Path $Root "app"
$configPath = Join-Path $Root "node.yaml"

foreach ($path in @($envFile, $python, $app, $configPath)) {
    if (-not (Test-Path $path)) { throw "Missing $path - run deploy\install-node.ps1 first, and create node.env." }
}

$settings = @{}
Get-Content $envFile | Where-Object { $_ -match "^\s*[^#].*=" } | ForEach-Object {
    $key, $value = $_ -split "=", 2
    $settings[$key.Trim()] = $value.Trim()
}

foreach ($required in @("ORCHESTRATOR_URL", "AGENT_BOOTSTRAP_TOKEN", "FOUNDRY_PROJECT_ENDPOINT")) {
    if (-not $settings[$required]) { throw "node.env is missing $required" }
}

$env:FOUNDRY_PROJECT_ENDPOINT = $settings["FOUNDRY_PROJECT_ENDPOINT"]
$env:FOUNDRY_DEFAULT_AGENT = $settings["FOUNDRY_DEFAULT_AGENT"]
$env:FOUNDRY_AGENT_MAP = $settings["FOUNDRY_AGENT_MAP"]
$env:AGENT_BOOTSTRAP_TOKEN = $settings["AGENT_BOOTSTRAP_TOKEN"]

# ---------------------------------------------------------------- config
$config = Get-Content $configPath -Raw
$current = if ($config -match "(?m)^orchestrator_url:\s*(\S+)") { $Matches[1] } else { "" }
if ($current -ne $settings["ORCHESTRATOR_URL"]) {
    Write-Host "== Updating orchestrator_url -> $($settings['ORCHESTRATOR_URL'])"
    $config = $config -replace "(?m)^orchestrator_url:.*$", "orchestrator_url: $($settings['ORCHESTRATOR_URL'])"
    Set-Content $configPath -Value $config -Encoding UTF8
}
$agentId = if ($config -match "(?m)^agent_id:\s*(\S+)") { $Matches[1] } else { "unknown" }

# ---------------------------------------------------------------- reachability
Write-Host "== Checking the orchestrator through the tunnel"
try {
    Invoke-RestMethod "$($settings['ORCHESTRATOR_URL'])/api/v1/models" -Headers @{ "X-API-Key" = "probe" } -ErrorAction Stop | Out-Null
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    switch ($code) {
        401 { Write-Host "   reachable (401 is expected for the probe key)" -ForegroundColor Green }
        404 { throw "404 - the tunnel has no port registered. On the laptop: devtunnel port create hecaton -p 8000 --protocol http" }
        502 { throw "502 - the tunnel is up but the orchestrator is not answering. Check its window on the laptop." }
        504 { throw "504 - 'devtunnel host' is not running on the laptop." }
        default { throw "Cannot reach $($settings['ORCHESTRATOR_URL']): $_" }
    }
}

# ---------------------------------------------------------------- bridge
$bridgeUp = $false
try {
    Invoke-RestMethod "http://127.0.0.1:$BridgePort/health" -TimeoutSec 3 | Out-Null
    $bridgeUp = $true
} catch { }

if ($bridgeUp) {
    Write-Host "== Bridge already running on $BridgePort"
} else {
    Write-Host "== Starting the Foundry bridge"
    $command = "`$env:FOUNDRY_PROJECT_ENDPOINT='$($settings['FOUNDRY_PROJECT_ENDPOINT'])'; " +
               "`$env:FOUNDRY_DEFAULT_AGENT='$($settings['FOUNDRY_DEFAULT_AGENT'])'; " +
               "`$env:FOUNDRY_AGENT_MAP='$($settings['FOUNDRY_AGENT_MAP'])'; " +
               "Set-Location '$app'; & '$python' -m uvicorn agent_bridge.foundry_bridge:app --host 127.0.0.1 --port $BridgePort"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $command

    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Seconds 2
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:$BridgePort/health" -TimeoutSec 3
            $bridgeUp = $true
        } catch { }
    } while (-not $bridgeUp -and (Get-Date) -lt $deadline)

    if (-not $bridgeUp) { throw "The bridge did not start. Check its window." }
    Write-Host "   agent: $($health.default_agent)" -ForegroundColor Green
}

# ---------------------------------------------------------------- node
Write-Host "== Starting the agent node ($agentId)"
$command = "`$env:AGENT_BOOTSTRAP_TOKEN='$($settings['AGENT_BOOTSTRAP_TOKEN'])'; " +
           "`$env:FOUNDRY_PROJECT_ENDPOINT='$($settings['FOUNDRY_PROJECT_ENDPOINT'])'; " +
           "Set-Location '$app'; & '$python' -m agent_node.runner --config '$configPath'"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $command

Write-Host ""
Write-Host "================ NODE STARTED ================" -ForegroundColor Green
Write-Host " agent_id     : $agentId"
Write-Host " orchestrator : $($settings['ORCHESTRATOR_URL'])"
Write-Host " bridge       : http://127.0.0.1:$BridgePort"
Write-Host ""
Write-Host " Verify from the laptop:" -ForegroundColor Cyan
Write-Host "   Invoke-RestMethod http://localhost:8000/api/v1/agents -Headers @{'X-API-Key'='<key>'}"
Write-Host "=============================================" -ForegroundColor Green
