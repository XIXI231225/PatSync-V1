param([string]$PythonPath = '', [switch]$InstallDependencies, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$packageRoot = $PSScriptRoot
$appRoot = Join-Path $packageRoot 'app'
$entry = Join-Path $appRoot 'start_demo.py'
$requirements = Join-Path $appRoot 'requirements.txt'
$diagnostic = Join-Path $packageRoot '启动日志.txt'

function Write-LaunchLog([string]$message) {
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $message
    try { Add-Content -LiteralPath $diagnostic -Value $line -Encoding UTF8 } catch { }
    Write-Host $message
}

try {
    Set-Location -LiteralPath $packageRoot
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw '找不到程序文件。请先完整解压整个参赛包，再运行启动演示.cmd。'
    }
    if (-not $PythonPath) { $PythonPath = $env:PATENT_DEMO_PYTHON }
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($PythonPath) {
        if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw '指定的 Python 不存在，请核对 PATENT_DEMO_PYTHON。' }
        $candidates.Add($PythonPath)
    } else {
        $candidates.Add((Join-Path $packageRoot '.venv\Scripts\python.exe'))
        $candidates.Add((Join-Path $packageRoot 'runtime\python\python.exe'))
        foreach ($registryRoot in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
            if (Test-Path -LiteralPath $registryRoot) {
                foreach ($versionKey in (Get-ChildItem -LiteralPath $registryRoot)) {
                    $installKey = Join-Path $versionKey.PSPath 'InstallPath'
                    if (Test-Path -LiteralPath $installKey) {
                        $properties = Get-ItemProperty -LiteralPath $installKey
                        if ($properties.ExecutablePath) { $candidates.Add([string]$properties.ExecutablePath) }
                        $installDirectory = (Get-Item -LiteralPath $installKey).GetValue('')
                        if ($installDirectory) { $candidates.Add((Join-Path $installDirectory 'python.exe')) }
                    }
                }
            }
        }
        foreach ($name in @('python.exe', 'python3.exe')) {
            foreach ($command in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
                if ($command.Source -and $command.Source -notmatch '\\WindowsApps\\') { $candidates.Add($command.Source) }
            }
        }
        # Codex is optional; its runtime is only the last local fallback.
        $candidates.Add((Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'))
    }
    $python = $null
    $missingHint = $null
    $probe = "import sys,json,importlib.util; print(json.dumps({'exe':sys.executable,'ok':sys.version_info>=(3,10),'version':sys.version.split()[0],'bits':64 if sys.maxsize>2**32 else 32,'missing':[m for m in ['openpyxl','pypdf','requests','cgi'] if importlib.util.find_spec(m) is None]}))"
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $raw = & $candidate -B -X utf8 -c $probe 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            $result = ($raw | Select-Object -Last 1) | ConvertFrom-Json
            if (-not $result.ok -or -not (Test-Path -LiteralPath $result.exe -PathType Leaf)) { continue }
            if (-not $InstallDependencies -and @($result.missing).Count -gt 0) {
                $missingHint = '缺少核心依赖：' + ($result.missing -join ', ') + '。请运行同目录的安装依赖.cmd，再重新启动。'
                continue
            }
            $python = [string]$result.exe
            break
        } catch { continue }
    }
    if (-not $python) {
        if ($missingHint) { throw $missingHint }
        throw '未找到可用的 Python 3.10 或更新版本。推荐安装 python.org 的 Python 3.12（64位），再运行安装依赖.cmd。'
    }
    if ($InstallDependencies) {
        $venv = Join-Path $packageRoot '.venv'
        $venvPython = Join-Path $venv 'Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            Write-LaunchLog '正在创建参赛包专用环境 .venv，不修改系统 Python 的依赖。'
            & $python -m venv $venv
            if ($LASTEXITCODE -ne 0) { throw '创建环境失败。请完整安装 Python（含 venv、pip），并将参赛包解压到可写目录。' }
        }
        Write-LaunchLog '正在安装依赖，需要联网；仅安装到本参赛包的 .venv。'
        $ErrorActionPreference = 'Continue'
        $installRequirements = $requirements
        $locked = Join-Path $appRoot 'requirements-lock-win-py312.txt'
        if ($result.version.StartsWith('3.12.') -and $result.bits -eq 64 -and (Test-Path -LiteralPath $locked)) { $installRequirements = $locked }
        & $venvPython -m pip install -r $installRequirements
        $installResult = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($installResult -ne 0) { throw '依赖安装失败，请检查网络后重新运行安装依赖.cmd。' }
        Write-LaunchLog '依赖安装完成。现在可以双击启动演示.cmd。'
        exit 0
    }
    Write-LaunchLog ('Python ' + $result.version + ' 核心依赖已验证：' + $python)
    if ($CheckOnly) { exit 0 }
    $port = 18879
    if ($env:DEMO_PATENT_PORT) { $port = [int]$env:DEMO_PATENT_PORT }
    if ($port -lt 1 -or $port -gt 65535) { throw 'DEMO_PATENT_PORT 必须是 1 至 65535 的整数。' }
    $url = 'http://127.0.0.1:' + $port
    $identityText = [System.IO.Path]::GetFullPath($appRoot).Replace('\','/').ToLowerInvariant()
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try { $identity = ([BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($identityText)))).Replace('-','').ToLowerInvariant() } finally { $hasher.Dispose() }
    $running = $null
    try { $running = Invoke-RestMethod -Uri ($url + '/api/contest') -TimeoutSec 2 } catch { }
    if ($running -and $running.instance_id -eq $identity -and $running.launcher_revision -eq '2026-10-31-empty') {
        Write-LaunchLog ('本目录的参赛演示已运行：' + $url)
        if ($env:DEMO_PATENT_NO_BROWSER -ne '1') { Start-Process -FilePath $url }
        exit 0
    }
    $portProbe = New-Object System.Net.Sockets.TcpClient
    try { $portProbe.Connect('127.0.0.1', $port); $occupied = $true } catch { $occupied = $false } finally { $portProbe.Close() }
    if ($occupied) { throw ('端口 ' + $port + ' 已被其他程序、其他目录或旧版演示占用。关闭对应的旧演示，或按运行环境说明修改 DEMO_PATENT_PORT。') }
    Write-LaunchLog ('正在启动：' + $url)
    Write-LaunchLog '启动后请保留此窗口；关闭窗口会停止演示。'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $ErrorActionPreference = 'Continue'
    & $python -B -X utf8 $entry
    $resultCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($resultCode -ne 0) { throw ('程序启动失败，返回码 ' + $resultCode + '。请查看窗口提示和 app/启动错误.log。') }
    exit 0
} catch {
    Write-LaunchLog ('启动失败：' + $_.Exception.Message)
    exit 1
}
