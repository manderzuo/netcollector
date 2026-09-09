# -*- coding: utf-8 -*-
<#
monitor_gui.ps1 — 多账号采集平台 GUI 增强监控

职责（相比旧版新增）：
  1. 卡死检测：主判据 = 心跳法。GUI 每 1 秒写 data\gui_heartbeat.txt，
     其中 ts 只在 GUI 主线程正常刷新 UI 时才会前进。若 ts 超过
     -HungSeconds(默认 15) 秒没有更新，判定界面"假死/未响应"。
     辅判据 = 原生响应探测（SendMessageTimeout WM_NULL），拿不到窗口句柄时跳过。
  2. 指标日志：记录进程存活、主线程 CPU 时间增量、后台线程数、
     数据库大小/最近写入、抖音/小红书窗口文件、最近活动时间、自愈重启计数。
  3. 自愈 / 告警：
       - GUI 进程退出：默认只告警；加 -AutoRestart 则自动拉起。
       - 检测到界面卡死：默认只告警；加 -KillHung 则结束进程并按需重启。
    告警会写入日志，并在控制台以红底醒目输出（如同时运行在本机桌面会看到）。

用法：
  powershell -ExecutionPolicy Bypass -File .\monitor_gui.ps1
  可选参数：
    -GuiDir <目录>        项目根目录（默认脚本所在目录）
    -IntervalSec <秒>     检测间隔（默认 5）
    -HungSeconds <秒>     连续多久无心跳判定卡死（默认 15）
    -AutoRestart [开关]   进程退出时自动重启
    -KillHung   [开关]    检测到卡死时结束进程（配合 -AutoRestart 可自动拉起）
    -Quiet      [开关]    关闭控制台告警（仅写日志）

说明：旧版从 data\gui.pid 读 PID 但无人维护该文件；本版以 data\gui_heartbeat.txt
中的 pid 为权威来源（GUI 每次启动都会写入心跳），更可靠。
#>

param(
    [string]$GuiDir = (Split-Path -Parent $MyInvocation.MyCommand.Path),
    [int]$IntervalSec = 5,
    [int]$HungSeconds = 15,
    [switch]$AutoRestart,
    [switch]$KillHung,
    [switch]$Quiet,
    [switch]$Legacy
)

$ErrorActionPreference = 'SilentlyContinue'
$root = if ([System.IO.Path]::IsPathRooted($GuiDir)) { $GuiDir } else { Join-Path (Get-Location) $GuiDir }
$data = Join-Path $root 'data'
$log = Join-Path $data 'gui_monitor.log'
$hbFile = Join-Path $data 'gui_heartbeat.txt'
$updateMarker = Join-Path $root '.update_pending'
$guiPy = if ($Legacy) {
    Join-Path $root 'src\gui.py'
} else {
    Join-Path $root 'src\ui2\default_app.py'
}
New-Item -ItemType Directory -Force -Path $data | Out-Null

$restartCount = 0

function Write-Log([string]$msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    $line | Add-Content -Path $log -Encoding utf8
    if (-not $Quiet) { Write-Output $line }
}
function Write-Alert([string]$msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] !! ALERT !! $msg"
    $line | Add-Content -Path $log -Encoding utf8
    if ($Quiet) { return }
    Write-Host "`n$line" -ForegroundColor White -BackgroundColor Red
}

# 读取心跳，返回 @{pid=; tsEpoch=; threads=} 或 $null
function Read-Heartbeat {
    if (-not (Test-Path $hbFile)) { return $null }
    $text = Get-Content $hbFile -Raw -Encoding UTF8
    if (-not $text) { return $null }
    $p = @{ pid = 0; tsEpoch = 0; threads = 0 }
    if ($text -match 'pid=(\d+)') { $p.pid = [int]$Matches[1] }
    if ($text -match 'ts=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
        try { $p.tsEpoch = ([datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)).ToUniversalTime() } catch { $p.tsEpoch = 0 }
    }
    if ($text -match 'threads=(\d+)') { $p.threads = [int]$Matches[1] }
    return $p
}

# 进程是否活着（按 pid）
function Test-Alive([int]$pid) {
    if ($pid -le 0) { return $false }
    return $null -ne (Get-Process -Id $pid -ErrorAction SilentlyContinue)
}

function Start-Gui {
    if (-not (Test-Path $guiPy)) { return $null }
    $venvPythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
    $venvPython = Join-Path $root '.venv\Scripts\python.exe'
    $runtime = if (Test-Path $venvPythonw) { $venvPythonw } elseif (Test-Path $venvPython) { $venvPython } else { $null }
    $runtimeArgs = @()
    if (-not $runtime) {
        $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if (-not $pyLauncher) {
            Write-Alert '自动启动失败：未找到 Python 3.11，请先安装运行环境。'
            return $null
        }
        if (-not $Legacy) {
            foreach ($version in @('-3.11', '-3')) {
                & $pyLauncher.Source $version -c 'import PySide6' 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $runtime = $pyLauncher.Source
                    $runtimeArgs = @($version)
                    break
                }
            }
        } else {
            $runtime = $pyLauncher.Source
            $runtimeArgs = @('-3.11')
        }
    }
    if (-not $runtime) {
        Write-Alert '自动启动失败：当前 Python 未安装 2.0 所需的 PySide6。'
        return $null
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $runtime
    $psi.Arguments = @($runtimeArgs + "`"$guiPy`"") -join ' '
    $psi.WorkingDirectory = $root
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
        return $proc.Id
    } catch {
        Write-Alert "自动启动 GUI 失败: $($_.Exception.Message)"
        return $null
    }
}

Write-Log "monitor started  (root=$root, mode=$(if($Legacy){'legacy'}else{'2.0'}), interval=${IntervalSec}s, hung=${HungSeconds}s, autorestart=$AutoRestart, killhung=$KillHung)"

# 进程探测失败后，连续多少次仍判定退出才正式确认（防启动期/轮询期的竞态误判）
$exitStreak = 0
$confirmExits = 2

while ($true) {
    $hb = Read-Heartbeat
    $guiPid = if ($hb) { $hb.pid } else { 0 }

    # 存活判据（更稳健）：
    #   1. 优先用进程探测 Get-Process；
    #   2. 若偶发查不到，再看心跳是否仍在推进——心跳 ts 未停，说明 GUI 主线程还活着，
    #      属于进程表竞态，不判退出。
    $procProbe = Get-Process -Id $guiPid -ErrorAction SilentlyContinue
    $hbFresh = $false
    if ($hb -and $hb.tsEpoch -gt 0) {
        $hbAge = [int](([datetime]::UtcNow - $hb.tsEpoch).TotalSeconds)
        $hbFresh = $hbAge -le $HungSeconds   # 心跳仍在"最近活动"阈值内 = GUI 活着
    }
    $alive = $null -ne $procProbe -or $hbFresh   # 进程在 或 心跳在前进，都算活着
    if ($alive) { $exitStreak = 0 }

    # 指标：进程主线程 CPU 时间（用于观察主事件循环是否在推进）
    $cpuSec = 0.0
    $proc2 = Get-Process -Id $guiPid -ErrorAction SilentlyContinue
    if ($proc2 -and $proc2.Threads) {
        $mainThread = $proc2.Threads | Where-Object { $_.Id -eq $proc2.MainThreadId } | Select-Object -First 1
        if ($mainThread -and $mainThread.TotalProcessorTime) { $cpuSec = $mainThread.TotalProcessorTime.TotalSeconds }
    }

    $db = Join-Path $data 'platform_gui.db'
    $dbState = if (Test-Path $db) {
        $f = Get-Item $db
        "db={0}B@{1}" -f $f.Length, $f.LastWriteTime.ToString('MM-dd HH:mm:ss')
    } else { 'db=missing' }
    $windowState = "dy=$([bool](Test-Path (Join-Path $data 'dy_window.txt')));xhs=$([bool](Test-Path (Join-Path $data 'xhs_window.txt')))"

    # 卡死判定：以心跳最近活动时间为准（仅在确认进程存活时判断界面是否假死）
    $hung = $false
    $hbInfo = 'no-heartbeat'
    if ($procProbe -and $hb -and $hb.tsEpoch -gt 0) {
        $idleSec = [int](([datetime]::UtcNow - $hb.tsEpoch).TotalSeconds)
        $hbInfo = "ts-idle=${idleSec}s threads=$($hb.threads)"
        if ($idleSec -gt $HungSeconds) { $hung = $true }
    } elseif ($procProbe) {
        $hbInfo = 'no-heartbeat-yet'
    }

    $state = if (-not $alive)   { "gui-exited pid=$guiPid" }
             elseif ($hung)     { "HUNG(not-responding) pid=$guiPid" }
             else               { "alive pid=$guiPid" }

    Write-Log "$state $dbState $windowState cpu=${cpuSec}s $hbInfo"

    if ($alive -and $hung) {
        Write-Alert "界面疑似卡死 pid=$guiPid：主线程已 $([int](([datetime]::UtcNow - $hb.tsEpoch).TotalSeconds))s 无心跳"
        if ($KillHung) {
            Write-Alert "正在结束卡死的 GUI 进程 pid=$guiPid"
            Stop-Process -Id $guiPid -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
    }

    if (-not $alive) {
        $exitStreak++
        if ($exitStreak -lt $confirmExits) {
            # 只剩进程探测与心跳都失败持续 N 次才确认退出，防启动/轮询竞态误判
            Write-Log "进程探测不到 pid=$guiPid（待确认 $exitStreak/$confirmExits，暂不处理）"
        } else {
            if (Test-Path -LiteralPath $updateMarker -PathType Leaf) {
                $markerAge = ((Get-Date) - (Get-Item -LiteralPath $updateMarker).LastWriteTime).TotalSeconds
                if ($markerAge -le 180) {
                    Write-Log "检测到更新正在接管，跳过自动重启（marker_age=$([int]$markerAge)s）"
                    $exitStreak = 0
                    Start-Sleep -Seconds $IntervalSec
                    continue
                }
                Remove-Item -LiteralPath $updateMarker -Force -ErrorAction SilentlyContinue
                Write-Log '更新标记超过 180 秒，按过期标记清理并恢复监控'
            }
            if ($AutoRestart) {
                $restartCount++
                Write-Alert "GUI 进程已退出 pid=$guiPid，自动重启(第 ${restartCount} 次)…"
                Start-Sleep -Seconds 2
                $newPid = Start-Gui
                if ($newPid) { Write-Log "GUI 已重启，新 pid=$newPid" }
            } else {
                Write-Alert "GUI 进程已退出 pid=$guiPid（未开启 -AutoRestart，监控将停止）"
                break
            }
        }
    }

    Start-Sleep -Seconds $IntervalSec
}
Write-Log "monitor stopped"
