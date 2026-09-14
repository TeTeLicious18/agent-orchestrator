<#
.SYNOPSIS
    One-shot startup for the control plane: loads secrets, starts the orchestrator and the tunnel.

.DESCRIPTION
    Generates deploy\.env on first run (API key, bootstrap token) and reuses it afterwards,
    so tokens stay stable across restarts and never have to be retyped.

    Opens two extra windows - one for uvicorn, one for the dev tunnel - waits until both
    answer, then prints the exact block to paste on each VM.

.EXAMPLE
    .\start-laptop.ps1
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$Python = "C:\venvs\hecaton\Scripts\python.exe",
    [string]$DbPath = "C:\hecaton-data\orchestrator.db",
    [string]$FoundryEndpoint = "https://hackathon-test-resource.services.ai.azure.com/api/projects/hackathon-test",
    [string]$TunnelName = "hecaton",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is empty when the script is dot-sourced or pasted, so fall back.
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot }
             elseif ($MyInvocation.MyCommand.Path) { Split-Path $MyInvocation.MyCommand.Path -Parent }
             else { (Get-Location).Path }

if (-not $RepoRoot) {
    $candidates = @((Split-Path $scriptDir -Parent), $scriptDir, (Get-Location).Path)
    $RepoRoot = $candidates | Where-Object { $_ -and (Test-Path (Join-Path $_ "orchestrator\main.py")) } | Select-Object -First 1
}
if (-not $RepoRoot) {
    throw "Could not locate the repository root. Pass it explicitly: .\start-laptop.ps1 -RepoRoot C:\path\to\hecaton"
}

$envFile = Join-Path $RepoRoot "deploy\.env"

function New-Secret { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ }) }

# ---------------------------------------------------------------- secrets
if (-not (Test-Path $envFile)) {
    Write-Host "== First run: generating secrets into $envFile" -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path (Split-Path $envFile -Parent) | Out-Null
    @(
        "ORCH_API_KEYS=$(New-Secret)",
        "ORCH_BOOTSTRAP_TOKEN=$(New-Secret)",
        "ORCH_DB_PATH=$DbPath",
        "FOUNDRY_PROJECT_ENDPOINT=$FoundryEndpoint"
    ) | Set-Content $envFile -Encoding UTF8
}

$settings = @{}
Get-Content $envFile | Where-Object { $_ -match "^\s*[^#].*=" } | ForEach-Object {
    $key, $value = $_ -split "=", 2
    $settings[$key.Trim()] = $value.Trim()
    Set-Item -Path "Env:$($key.Trim())" -Value $value.Trim()
}
$env:ORCH_FOUNDRY_CATALOG_FILE = ""

New-Item -ItemType Directory -Force -Path (Split-Path $settings["ORCH_DB_PATH"] -Parent) | Out-Null

# ---------------------------------------------------------------- azure
Write-Host "== Checking Azure sign-in"
$account = az account show --query name -o tsv 2>$null
if (-not $account) {
    Write-Host "   not signed in, launching az login"
    az login | Out-Null
    $account = az account show --query name -o tsv
}
Write-Host "   subscription: $account"

# ---------------------------------------------------------------- orchestrator
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "== Orchestrator already listening on $Port"
} else {
    Write-Host "== Starting orchestrator"
    $envAssignments = ($settings.GetEnumerator() | ForEach-Object { "`$env:$($_.Key)='$($_.Value)'" }) -join "; "
    $command = "$envAssignments; `$env:ORCH_FOUNDRY_CATALOG_FILE=''; Set-Location '$RepoRoot'; & '$Python' -m uvicorn orchestrator.main:app --host 0.0.0.0 --port $Port"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $command
}

$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 2
    try {
        $models = Invoke-RestMethod "http://localhost:$Port/api/v1/models" -Headers @{ "X-API-Key" = $settings["ORCH_API_KEYS"] }
        $ready = $true
    } catch { $ready = $false }
} while (-not $ready -and (Get-Date) -lt $deadline)

if (-not $ready) { throw "Orchestrator did not become healthy on port $Port. Check its window." }
Write-Host "   catalog: $(($models | ForEach-Object { $_.name }) -join ', ')" -ForegroundColor Green

# ---------------------------------------------------------------- tunnel
Write-Host "== Starting dev tunnel"

# winget puts devtunnel on PATH only for sessions started after the install, and
# Start-Process inherits this session's PATH, so resolve the executable up front.
$devtunnel = (Get-Command devtunnel -ErrorAction SilentlyContinue).Source
if (-not $devtunnel) {
    $devtunnel = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\devtunnel.exe",
        "$env:ProgramFiles\Microsoft\DevTunnels\devtunnel.exe",
        "C:\tools\devtunnel.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $devtunnel) {
    throw "devtunnel not found. Install it with: winget install Microsoft.devtunnel"
}

if (-not (& $devtunnel show $TunnelName 2>$null)) {
    & $devtunnel create $TunnelName --allow-anonymous | Out-Null
}
# The port must be declared as http: uvicorn serves plain HTTP behind the tunnel's TLS.
& $devtunnel port create $TunnelName -p $Port --protocol http 2>$null | Out-Null

$hosted = ((& $devtunnel show $TunnelName) -join "`n") -match "Host connections\s*:\s*([1-9])"
if ($hosted) {
    Write-Host "   already hosted"
} else {
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$devtunnel' host $TunnelName"
}

$tunnelUrl = $null
$reachable = $false
$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 3
    $show = (& $devtunnel show $TunnelName 2>$null) -join "`n"
    if ($show -match "https://[a-z0-9]+-$Port\.[a-z0-9]+\.devtunnels\.ms") { $tunnelUrl = $Matches[0] }
    if (-not $tunnelUrl -and $show -match "Tunnel ID\s*:\s*([a-z0-9]+)\.([a-z0-9]+)") {
        $tunnelUrl = "https://$($Matches[1])-$Port.$($Matches[2]).devtunnels.ms"
    }
    if ($tunnelUrl) {
        try {
            $probe = Invoke-WebRequest "$tunnelUrl/api/v1/models" -Headers @{ "X-API-Key" = $settings["ORCH_API_KEYS"] } -UseBasicParsing
            # A hosted tunnel returns the payload; an unhosted one answers 200 with an empty body.
            $reachable = $probe.RawContentLength -gt 0
        } catch { $reachable = $false }
    }
} while (-not $reachable -and (Get-Date) -lt $deadline)

if (-not $reachable) {
    Write-Warning "Tunnel not serving yet. Check the 'devtunnel host' window, then re-run."
    if ($tunnelUrl) { Write-Host "   detected URL: $tunnelUrl" }
    return
}

# ---------------------------------------------------------------- summary
Write-Host ""
Write-Host "================ CONTROL PLANE READY ================" -ForegroundColor Green
Write-Host " Dashboard : http://localhost:$Port/dashboard/"
Write-Host " API key   : $($settings['ORCH_API_KEYS'])"
Write-Host " Tunnel    : $tunnelUrl"
Write-Host ""
Write-Host " Paste this on each VM before running start-vm.bat:" -ForegroundColor Cyan
Write-Host ""
Write-Host "   @'"
Write-Host "   ORCHESTRATOR_URL=$tunnelUrl"
Write-Host "   AGENT_BOOTSTRAP_TOKEN=$($settings['ORCH_BOOTSTRAP_TOKEN'])"
Write-Host "   FOUNDRY_PROJECT_ENDPOINT=$($settings['FOUNDRY_PROJECT_ENDPOINT'])"
Write-Host "   FOUNDRY_DEFAULT_AGENT=john-agent"
Write-Host "   '@ | Set-Content C:\hecaton\node.env -Encoding UTF8"
Write-Host ""
Write-Host " Then run the demo here:" -ForegroundColor Cyan
Write-Host "   .\tools\demo-workflow.ps1 -ApiKey `"$($settings['ORCH_API_KEYS'])`""
Write-Host "=====================================================" -ForegroundColor Green
