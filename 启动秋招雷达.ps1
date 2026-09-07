$ErrorActionPreference = 'Stop'

$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$serverScript = Join-Path $root 'server.py'
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
}
if (-not $pythonCommand) {
    Write-Error '找不到 Python。请先安装 Python 3.11 或更高版本。'
    exit 1
}

$rootKey = $root.ToLowerInvariant().Replace('/', '\')
$currentListeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 5500 -State Listen -ErrorAction SilentlyContinue)
$currentOwnerIds = @($currentListeners | Select-Object -ExpandProperty OwningProcess -Unique)
$proxyListeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
$proxyOwnerIds = @($proxyListeners | Select-Object -ExpandProperty OwningProcess -Unique)
$pythonProcesses = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue)
$owned = @($pythonProcesses | Where-Object {
    $commandLine = ([string]$_.CommandLine).ToLowerInvariant().Replace('/', '\')
    ($commandLine.Contains($rootKey) -or $currentOwnerIds -contains ([int]$_.ProcessId)) -and $commandLine -match 'server\.py'
})
$ownedProxy = @($pythonProcesses | Where-Object {
    $commandLine = ([string]$_.CommandLine).ToLowerInvariant().Replace('/', '\')
    ($commandLine.Contains($rootKey) -or $proxyOwnerIds -contains ([int]$_.ProcessId)) -and $commandLine -match 'local_ai_proxy\.py'
})
foreach ($processInfo in $owned) {
    try { Stop-Process -Id ([int]$processInfo.ProcessId) -Force -ErrorAction Stop } catch { }
}
foreach ($processInfo in $ownedProxy) {
    try { Stop-Process -Id ([int]$processInfo.ProcessId) -Force -ErrorAction Stop } catch { }
}
if ($owned.Count -gt 0 -or $ownedProxy.Count -gt 0) { Start-Sleep -Milliseconds 500 }

$listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 5500 -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    $ownerIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    $ownerDescriptions = @($ownerIds | ForEach-Object {
        $item = Get-CimInstance Win32_Process -Filter "ProcessId=$($_)" -ErrorAction SilentlyContinue
        if ($item) { "PID $($_): $($item.CommandLine)" } else { "PID $_" }
    })
    Write-Error ("127.0.0.1:5500 已被其他程序占用。请关闭后重试。`n" + ($ownerDescriptions -join "`n"))
    exit 1
}

$stdoutLog = Join-Path $root 'server.stdout.log'
$stderrLog = Join-Path $root 'server.stderr.log'
$process = Start-Process -FilePath $pythonCommand.Source -ArgumentList @('-u', $serverScript) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru

$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $health = Invoke-WebRequest -Uri 'http://127.0.0.1:5500/api/health' -UseBasicParsing -TimeoutSec 2
        if ($health.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    if ($process.HasExited) { break }
}

if (-not $ready) {
    $errorTail = if (Test-Path -LiteralPath $stderrLog) { Get-Content -LiteralPath $stderrLog -Tail 12 -ErrorAction SilentlyContinue } else { @() }
    if ($process.HasExited) {
        Write-Error ("秋招雷达启动失败（退出码 $($process.ExitCode)）。`n" + ($errorTail -join "`n"))
    } else {
        Write-Error ("秋招雷达未在 30 秒内就绪。`n" + ($errorTail -join "`n"))
    }
    exit 1
}

Write-Host '秋招雷达已启动：http://127.0.0.1:5500/'

# Keep the former 8765 endpoint usable for older clients. A foreign process on
# that port is left untouched; it cannot affect the main site.
$proxyPortBusy = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
if ($proxyPortBusy.Count -eq 0) {
    $proxyScript = Join-Path $root 'local_ai_proxy.py'
    $proxyOut = Join-Path $root 'proxy.stdout.log'
    $proxyErr = Join-Path $root 'proxy.stderr.log'
    Start-Process -FilePath $pythonCommand.Source -ArgumentList @('-u', $proxyScript) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $proxyOut -RedirectStandardError $proxyErr | Out-Null
}
Start-Process 'http://127.0.0.1:5500/'
