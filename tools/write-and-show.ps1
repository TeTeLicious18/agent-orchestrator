<#
.SYNOPSIS
    Generates text with a Foundry model, saves it on the node and opens it in Notepad there.

.DESCRIPTION
    Chains three orchestrator tasks. Each one is routed independently, so this also
    demonstrates that a node can be driven to produce a visible side effect on its own
    desktop without anything extra installed on it.

      1. foundry.chat  - the bound model deployment writes the text
      2. fs.write      - the node stores it inside its sandboxed workspace
      3. shell.exec    - an allow-listed command opens it in Notepad

    Step 3 requires the 'open_notepad' entry in the node's allowed_commands, and the node
    must be running in an interactive session for the window to be visible.

.EXAMPLE
    .\write-and-show.ps1 -ApiKey "<key>" -Prompt "Write three haikus about Azure virtual machines."
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ApiKey,
    [string]$Prompt = "Write three short poems about autonomous agents running on virtual machines.",
    [string]$System = "You are a poet. Reply with the poems only, no preamble and no commentary.",
    [string]$FileName = "poems/poem.txt",
    [string]$BaseUrl = "http://localhost:8000",
    [string]$AgentId,
    [switch]$SkipNotepad,
    [int]$StepTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$headers = @{ "X-API-Key" = $ApiKey; "Content-Type" = "application/json" }

function Invoke-OrchestratorTask {
    param([hashtable]$Submission, [string]$Label)

    if ($AgentId) { $Submission.target_agent_id = $AgentId }
    $task = Invoke-RestMethod -Method Post "$BaseUrl/api/v1/tasks" `
        -Headers $headers -Body ($Submission | ConvertTo-Json -Depth 8)

    Write-Host "   $Label -> $($task.task_id)"
    $deadline = (Get-Date).AddSeconds($StepTimeoutSeconds)
    do {
        Start-Sleep -Seconds 2
        $view = Invoke-RestMethod "$BaseUrl/api/v1/tasks/$($task.task_id)" -Headers $headers
    } while ($view.status -in @("pending", "assigned", "running", "blocked") -and (Get-Date) -lt $deadline)

    if ($view.status -ne "succeeded") {
        $events = Invoke-RestMethod "$BaseUrl/api/v1/tasks/$($task.task_id)/events" -Headers $headers
        $events | Select-Object -Last 3 | ForEach-Object { Write-Host "      $($_.message)" -ForegroundColor DarkGray }
        throw "$Label ended as '$($view.status)': $($view.error)"
    }
    return $view
}

Write-Host "== 1/3 Generating text" -ForegroundColor Cyan
$generated = Invoke-OrchestratorTask -Label "foundry.chat" -Submission @{
    action                      = "foundry.chat"
    title                       = "Generate text"
    payload                     = @{ system = $System; prompt = $Prompt }
    required_model_capabilities = @("chat")
}

$text = $generated.result.content
if (-not $text) { throw "The model returned no content." }
Write-Host "   deployment: $($generated.result.deployment)  chars: $($text.Length)" -ForegroundColor Green
Write-Host ""
Write-Host $text -ForegroundColor DarkGray
Write-Host ""

Write-Host "== 2/3 Writing the file on the node" -ForegroundColor Cyan
$written = Invoke-OrchestratorTask -Label "fs.write" -Submission @{
    action  = "fs.write"
    title   = "Save generated text"
    payload = @{ path = $FileName; content = $text }
}
Write-Host "   $($written.result.bytes_written) bytes -> $($written.result.path)" -ForegroundColor Green

if ($SkipNotepad) { return }

Write-Host "== 3/3 Opening Notepad on the node" -ForegroundColor Cyan
$opened = Invoke-OrchestratorTask -Label "shell.exec" -Submission @{
    action  = "shell.exec"
    title   = "Open in Notepad"
    payload = @{ command = "open_notepad"; args = @($FileName) }
}
Write-Host "   $($opened.result.stdout.Trim())" -ForegroundColor Green

Write-Host ""
Write-Host "Done - check the VM's desktop." -ForegroundColor Green
