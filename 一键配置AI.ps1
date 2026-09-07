# CCSwitch owns the provider credentials. This shortcut only starts the local
# service and opens the working site; it never asks for or stores a key.
$ErrorActionPreference = 'Stop'
$startup = Join-Path $PSScriptRoot '启动秋招雷达.ps1'
if (-not (Test-Path -LiteralPath $startup)) {
    Write-Error '找不到启动秋招雷达.ps1'
    exit 1
}
& $startup
