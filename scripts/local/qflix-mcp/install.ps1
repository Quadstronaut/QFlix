# scripts/local/qflix-mcp/install.ps1
# Creates a venv, installs the MCP SDK, prints the Claude Code registration command.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $here ".venv"

if (-not (Test-Path "$venv\Scripts\python.exe")) {
    Write-Host "Creating venv at $venv"
    & python -m venv $venv
}

Write-Host "Installing requirements"
& "$venv\Scripts\python.exe" -m pip install -q -r (Join-Path $here "requirements.txt")

$entryPoint = Join-Path $here "qflix_mcp.py"
$pythonExe  = Join-Path $venv "Scripts\python.exe"

Write-Host ""
Write-Host "Now register with Claude Code:"
Write-Host "  claude mcp add qflix-mcp -- `"$pythonExe`" `"$entryPoint`""
Write-Host ""
Write-Host "Verify:"
Write-Host "  claude mcp list"
