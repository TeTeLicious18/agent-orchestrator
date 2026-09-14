<#
.SYNOPSIS
    Opens a file from the agent workspace in Notepad. Invoked through the node's shell allow-list.

.DESCRIPTION
    The allow-list in node.yaml pins the argv prefix down to "-File <this script>", so a task
    can only supply the arguments below - never an arbitrary command. This script is therefore
    the actual trust boundary: it refuses absolute paths, UNC paths and any path that resolves
    outside the workspace root.

    Notepad only becomes visible when the node runs inside an interactive desktop session.
    A node running as a Windows service lives in session 0 and will start Notepad invisibly.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RelativePath,
    [string]$WorkspaceRoot = "C:\hecaton\workspace"
)

$ErrorActionPreference = "Stop"

if ([System.IO.Path]::IsPathRooted($RelativePath) -or $RelativePath.StartsWith("\\")) {
    throw "Only paths relative to the workspace are allowed."
}

$root = (Resolve-Path $WorkspaceRoot).Path
$target = [System.IO.Path]::GetFullPath((Join-Path $root $RelativePath))

if (-not $target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Path escapes the workspace sandbox."
}
if (-not (Test-Path $target -PathType Leaf)) {
    throw "File not found: $RelativePath"
}

Start-Process notepad.exe -ArgumentList $target
Write-Output (@{ opened = $target; bytes = (Get-Item $target).Length } | ConvertTo-Json -Compress)
