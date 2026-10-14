<#
.SYNOPSIS
    OfficeFlow 启动 / 停止 / 状态管理脚本（Windows PowerShell）。

.DESCRIPTION
    统一管理后端（FastAPI :8000）与前端（Streamlit :8501）。
    - 所有命令都使用项目自带 .venv，不依赖 conda。
    - PID 记录在 .run\ 下，停止前会校验进程命令行，避免误杀同 PID 的其它进程。

.EXAMPLE
    .\scripts\start.ps1 start            # 启动后端 + 前端
    .\scripts\start.ps1 status           # 查看状态与端口占用
    .\scripts\start.ps1 stop             # 停止全部
    .\scripts\start.ps1 start-backend    # 只启动后端
    .\scripts\start.ps1 check            # 运行 LangExtract 自检

.NOTES
    需在项目根目录执行；脚本会自行定位根目录，因此从任意位置调用也可以。
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'start-backend', 'start-frontend', 'stop', 'status', 'check', 'test')]
    [string]$Action = 'start',

    [int]$BackendPort = 8000,
    [int]$FrontendPort = 8501
)

$ErrorActionPreference = 'Stop'

# 本脚本含中文，文件必须保存为 UTF-8 with BOM —— Windows PowerShell 5.1 默认按
# ANSI/GBK 读取无 BOM 的 .ps1，汉字会被解码错乱并产生伪 '}'，导致解析失败。
# 同时把控制台编码设为 UTF-8，避免运行期中文输出乱码。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
    Write-Warning "控制台 UTF-8 设置失败，中文输出可能乱码：$($_.Exception.Message)"
}
$env:PYTHONIOENCODING = 'utf-8'    # 让子进程（uvicorn/streamlit）用 UTF-8 输出

# ---------- 路径定位（以脚本位置推导项目根，避免依赖 cwd）----------
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$RunDir = Join-Path $Root '.run'
$BackendPidFile = Join-Path $RunDir 'backend.pid'
$FrontendPidFile = Join-Path $RunDir 'frontend.pid'
$BackendLog = Join-Path $RunDir 'backend.log'
$FrontendLog = Join-Path $RunDir 'frontend.log'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "[X] $msg" -ForegroundColor Red }

function Assert-Env {
    if (-not (Test-Path $Py)) {
        Write-Err "找不到虚拟环境解释器：$Py"
        Write-Host "    请先创建 .venv 并安装依赖，见 docs\部署与启动.md §1" -ForegroundColor Yellow
        exit 1
    }
    if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir | Out-Null }
}

function Get-PortOwner([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($conn) { return $conn.OwningProcess }
    }
    catch { }
    return $null
}

function Get-SavedPid($PidFile) {
    if (-not (Test-Path $PidFile)) { return $null }
    $raw = (Get-Content $PidFile -Raw).Trim()
    if ($raw -match '^\d+$') { return [int]$raw }
    return $null
}

function Test-ProcessMatch([int]$ProcessId, [string]$Pattern) {
    $p = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId").CommandLine
        return ($cmd -and $cmd -match $Pattern)
    }
    catch { return $true }   # 拿不到命令行时保守认为匹配
}

function Stop-ProcessForce([int]$ProcessId) {
    <#
      结束进程，两级尝试。
      实测：在受限安全上下文里 Stop-Process / taskkill 均报 "Access is denied"，
      而同一个进程用 WMI 的 Terminate 可以成功 —— 所以必须保留这个兜底。
      返回 $true 表示"已不存在"。
    #>
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        return $true
    }
    catch {
        # 落到 WMI 兜底
    }
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
        if (-not $proc) { return $true }
        $result = Invoke-CimMethod -InputObject $proc -MethodName Terminate -ErrorAction Stop
        return ($result.ReturnValue -eq 0)
    }
    catch {
        return $false
    }
}

function Get-ProcessVerdict([int]$ProcessId, [string]$Pattern) {
    <#
      三态判定进程是否属于本项目的服务：
        'match'    —— 命令行匹配（或可执行文件在项目 .venv 下）
        'mismatch' —— 能读到命令行但明显不是我们的进程（PID 被复用）→ 绝不误杀
        'unknown'  —— 读不到进程元数据（权限受限环境）→ 由调用方决定宽松或严格
    #>
    $ci = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if (-not $ci) { return 'mismatch' }

    $cmd = [string]$ci.CommandLine
    $exe = [string]$ci.ExecutablePath

    if (-not $cmd -and -not $exe) { return 'unknown' }
    if ($cmd -match $Pattern) { return 'match' }
    if ($exe -like "*$Root\.venv\*") { return 'match' }
    return 'mismatch'
}

function Stop-Managed($Name, $PidFile, $Pattern, [int]$Port = 0) {
    # 以"端口占用者"为权威目标：PID 文件可能过期或被系统复用（实测发生过 ——
    # 记录的 PID 指向了别的进程，导致旧逻辑拒绝结束且不回退，服务停不掉）。
    $targets = @()

    if ($Port -gt 0) {
        $owner = Get-PortOwner $Port
        if ($owner) {
            $verdict = Get-ProcessVerdict $owner $Pattern
            if ($verdict -eq 'match') {
                $targets += $owner
            }
            elseif ($verdict -eq 'unknown') {
                # 端口是这个端口、进程名是 python —— 在权限受限环境里允许结束，但明确告知
                $pname = (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName
                if ($pname -like 'python*') {
                    Write-Warn2 "端口 $Port 被 PID $owner ($pname) 占用；当前环境读不到其命令行，按端口判定为本项目服务"
                    $targets += $owner
                }
                else {
                    Write-Warn2 "端口 $Port 被 PID $owner ($pname) 占用，不是 python 进程，拒绝结束"
                }
            }
            else {
                Write-Warn2 "端口 $Port 被 PID $owner 占用，但它不是本项目的 $Pattern 进程，拒绝结束"
            }
        }
        else {
            Write-Warn2 "$Name 端口 $Port 无占用（服务未运行）"
        }
    }

    # PID 文件里的包装进程（venv 启动器）也一并结束，避免残留。
    # 这一项用**严格档**：端口占用者已经是权威目标，包装进程只是清理，
    # 读不到元数据时宁可不杀（避免误杀被复用 PID 的无关进程）。
    $saved = Get-SavedPid $PidFile
    if ($saved -and (Get-Process -Id $saved -ErrorAction SilentlyContinue)) {
        $verdict = Get-ProcessVerdict $saved $Pattern
        if ($verdict -eq 'match') {
            $targets += $saved
        }
        elseif ($verdict -eq 'mismatch') {
            Write-Warn2 "PID 文件记录的 $saved 不是本项目的 $Pattern 进程（PID 可能已被系统复用），忽略"
        }
        else {
            Write-Warn2 "PID 文件记录的 $saved 无法核实身份，保守跳过（端口占用者已处理）"
        }
    }

    $targets = $targets | Sort-Object -Unique
    if (-not $targets) {
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }

    foreach ($procId in $targets) {
        Write-Step "停止 $Name (PID $procId)"

        # python -m uvicorn / -m streamlit 会派生子进程（子进程用基础解释器），
        # 先结束"父包装进程"再结束子进程，避免父进程存活导致端口未释放。
        $ci = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
        if ($ci -and $ci.ParentProcessId -and $ci.ParentProcessId -ne 0) {
            $par = Get-CimInstance Win32_Process -Filter "ProcessId = $($ci.ParentProcessId)" -ErrorAction SilentlyContinue
            if ($par) {
                $pcmd = [string]$par.CommandLine
                if ($pcmd -like '*uvicorn*' -or $pcmd -like '*streamlit*') {
                    [void](Stop-ProcessForce $par.ProcessId)
                }
            }
        }

        if (-not (Stop-ProcessForce $procId)) {
            Write-Warn2 "$Name (PID $procId) 无法结束（可能需要管理员权限）"
        }
    }

    # 等端口真正释放（最多 5 秒），释放不掉再兜底强杀
    if ($Port -gt 0) {
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Milliseconds 500
            if (-not (Get-PortOwner $Port)) { break }
        }
        if (Get-PortOwner $Port) {
            $leftover = Get-PortOwner $Port
            Write-Warn2 "$Name 端口 $Port 仍被 PID $leftover 占用，强制结束"
            [void](Stop-ProcessForce $leftover)
        }
    }

    Remove-Item $PidFile -ErrorAction SilentlyContinue
    if ($Port -gt 0 -and (Get-PortOwner $Port)) {
        Write-Err "$Name 停止失败，端口 $Port 仍被占用"
    }
    else {
        Write-Ok "$Name 已停止，端口 $Port 已释放"
    }
}

function Start-Service([string]$Name, [string]$PidFile, [string]$LogFile, [int]$Port, [string[]]$PyArgs, [string]$Pattern) {
    $existing = Get-PortOwner $Port
    if ($existing) {
        Write-Warn2 "$Name 端口 $Port 已被 PID $existing 占用，跳过启动"
        return
    }

    Write-Step "启动 $Name（端口 $Port）"
    $argList = @($PyArgs)
    $proc = Start-Process -FilePath $Py -ArgumentList $argList `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $LogFile -RedirectStandardError "$LogFile.err"
    $proc.Id | Set-Content $PidFile -Encoding ascii
    Write-Ok "$Name 已启动，PID $($proc.Id)，日志：$LogFile"

    # 等待端口就绪（最多 ~15 秒）
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Get-PortOwner $Port) { Write-Ok "$Name 已监听 $Port"; return }
        if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
            Write-Err "$Name 启动后立即退出，请查看日志：$LogFile"
            return
        }
    }
    Write-Warn2 "$Name 15 秒内未监听 $Port，请查看日志：$LogFile"
}

function Test-IsDescendantOf([int]$Child, [int]$Ancestor) {
    <#
      python -m uvicorn / -m streamlit 会派生子进程（子进程用基础解释器），
      因此"端口占用者"通常是"记录的 PID"的子进程，属正常情况，不应告警。
    #>
    $cur = $Child
    for ($i = 0; $i -lt 5; $i++) {
        if ($cur -eq $Ancestor) { return $true }
        $ci = Get-CimInstance Win32_Process -Filter "ProcessId = $cur" -ErrorAction SilentlyContinue
        if (-not $ci -or -not $ci.ParentProcessId -or $ci.ParentProcessId -eq 0) { return $false }
        $cur = $ci.ParentProcessId
    }
    return $false
}

function Show-Status {
    Write-Host ""
    Write-Host "OfficeFlow 运行状态" -ForegroundColor Cyan
    Write-Host ("-" * 58)

    foreach ($svc in @(
            @{ Name = '后端 FastAPI'; Port = $BackendPort; PidFile = $BackendPidFile; Url = "http://127.0.0.1:$BackendPort/docs" },
            @{ Name = '前端 Streamlit'; Port = $FrontendPort; PidFile = $FrontendPidFile; Url = "http://localhost:$FrontendPort" }
        )) {
        $procId = Get-SavedPid $svc.PidFile
        $owner = Get-PortOwner $svc.Port
        $state = if ($owner) { "运行中 (PID $owner)" } else { "未运行" }
        Write-Host ("  {0,-16} 端口 {1,-6} {2}" -f $svc.Name, $svc.Port, $state)
        if ($owner) { Write-Host ("  {0,-16} {1}" -f '', $svc.Url) -ForegroundColor DarkGray }
        # 仅在"占用者既不是记录的 PID、也不是其后代"时才告警（真正被别的程序占用）
        if ($procId -and $owner -and $procId -ne $owner -and
            -not (Test-IsDescendantOf -Child $owner -Ancestor $procId)) {
            Write-Warn2 "  端口 $($svc.Port) 被 PID $owner 占用，它不是本脚本启动的进程（记录 PID $procId）"
        }
    }

    Write-Host ("-" * 58)
    $apiKey = if ($env:OFFICEFLOW_MODEL_API_KEY) { '已设置' } else { '未设置（将走规则引擎）' }
    $lxEnable = if ($env:OFFICEFLOW_LANGEXTRACT_ENABLE) { $env:OFFICEFLOW_LANGEXTRACT_ENABLE } else { '0（未开启）' }
    $envFile = Join-Path $Root '.env'
    Write-Host "  .env 文件        $(if (Test-Path $envFile) { '存在' } else { '不存在（可选）' })"
    Write-Host "  API Key          $apiKey"
    Write-Host "  LangExtract 开关 $lxEnable"
    Write-Host ""
    Write-Host "  提示：.env 只需写入文件，重启后端即生效；本 shell 的变量状态不受影响。" -ForegroundColor DarkGray
    Write-Host ""
}

Assert-Env

switch ($Action) {
    'start' {
        Write-Host "OfficeFlow 启动" -ForegroundColor Cyan
        Start-Service -Name '后端 FastAPI' -PidFile $BackendPidFile -LogFile $BackendLog -Port $BackendPort `
            -PyArgs @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$BackendPort") `
            -Pattern 'uvicorn'
        Start-Service -Name '前端 Streamlit' -PidFile $FrontendPidFile -LogFile $FrontendLog -Port $FrontendPort `
            -PyArgs @('-m', 'streamlit', 'run', 'frontend\app.py', '--server.port', "$FrontendPort", '--server.headless', 'true') `
            -Pattern 'streamlit'
        Show-Status
        Write-Host "浏览器访问：http://localhost:$FrontendPort" -ForegroundColor Green
        Write-Host "停止服务：  .\scripts\start.ps1 stop" -ForegroundColor DarkGray
    }
    'start-backend' {
        Start-Service -Name '后端 FastAPI' -PidFile $BackendPidFile -LogFile $BackendLog -Port $BackendPort `
            -PyArgs @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$BackendPort") `
            -Pattern 'uvicorn'
    }
    'start-frontend' {
        Start-Service -Name '前端 Streamlit' -PidFile $FrontendPidFile -LogFile $FrontendLog -Port $FrontendPort `
            -PyArgs @('-m', 'streamlit', 'run', 'frontend\app.py', '--server.port', "$FrontendPort", '--server.headless', 'true') `
            -Pattern 'streamlit'
    }
    'stop' {
        Stop-Managed -Name '前端 Streamlit' -PidFile $FrontendPidFile -Pattern 'streamlit' -Port $FrontendPort
        Stop-Managed -Name '后端 FastAPI' -PidFile $BackendPidFile -Pattern 'uvicorn' -Port $BackendPort
        Show-Status
    }
    'status' { Show-Status }
    'check' {
        Write-Step 'LangExtract 环境自检'
        & $Py (Join-Path $Root 'scripts\check_langextract.py')
    }
    'test' {
        Write-Step '运行后端测试'
        & $Py -m pytest (Join-Path $Root 'tests') -v
    }
}
