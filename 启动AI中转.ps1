# Compatibility entry point kept for older shortcuts. The website and its AI
# assistant now share the single local backend on port 5500.
$ErrorActionPreference = 'Stop'
$startup = Join-Path $PSScriptRoot '启动秋招雷达.ps1'
if (-not (Test-Path -LiteralPath $startup)) {
    Write-Error '找不到启动秋招雷达.ps1'
    exit 1
}
& $startup
