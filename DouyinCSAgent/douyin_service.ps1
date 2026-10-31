param(
    [ValidateSet("start", "stop", "status", "logs")]
    [string]$Action = "status",
    [switch]$NoSend,
    [string]$Cdp = "http://127.0.0.1:9224",
    [int]$Port = 8787,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$AgentDir = $PSScriptRoot
$GuiRoot = Split-Path -Parent $AgentDir
$Workspace = Split-Path -Parent $GuiRoot
$CarAgentRoot = Join-Path $Workspace "car-agent"
$LogDir = Join-Path $GuiRoot "exports\douyin_service_logs"

function Get-LocalServerProcess {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match "local_carzone_compat_server.py" -and $_.CommandLine -match "--port $Port" }
}

function Get-WatcherProcess {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match "douyin_current_chat_reply.py" -and $_.CommandLine -match "--watch" }
}

function Start-LocalServer {
    $listener = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" } |
        Select-Object -First 1
    if ($listener) {
        Write-Output "local_carzone already listening on 127.0.0.1:$Port pid=$($listener.OwningProcess)"
        return
    }

    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    if (-not $env:DOUYIN_REPLY_LLM_ENABLED) {
        $env:DOUYIN_REPLY_LLM_ENABLED = "1"
    }
    if (-not $env:DOUYIN_REPLY_WEB_SEARCH_ENABLED) {
        $env:DOUYIN_REPLY_WEB_SEARCH_ENABLED = "1"
    }
    if (-not $env:DOUYIN_PURE_LLM_BASELINE_ENABLED) {
        $env:DOUYIN_PURE_LLM_BASELINE_ENABLED = "1"
    }
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $out = Join-Path $LogDir "local_$stamp.out.log"
    $err = Join-Path $LogDir "local_$stamp.err.log"
    $args = @(
        "DouyinCSAgent\local_carzone_compat_server.py",
        "--host", "127.0.0.1",
        "--port", "$Port",
        "--car-agent-root", $CarAgentRoot
    )
    $p = Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $GuiRoot -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    Start-Sleep -Seconds 1
    $replyMode = if ($env:DOUYIN_PURE_LLM_BASELINE_ENABLED -eq "1" -and ($env:ARK_API_KEY -or $env:VOLCENGINE_API_KEY -or $env:DOUBAO_API_KEY -or $env:DOUYIN_REPLY_LLM_API_KEY -or $env:DOUYIN_BASELINE_LLM_API_KEY)) { "pure-llm-baseline" } else { "unavailable" }
    Write-Output "started local_carzone pid=$($p.Id) reply_mode=$replyMode log=$err"
}

function Start-Watcher {
    $existing = Get-WatcherProcess | Select-Object -First 1
    if ($existing) {
        Write-Output "douyin watcher already running pid=$($existing.ProcessId)"
        return
    }

    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $out = Join-Path $LogDir "watch_$stamp.out.log"
    $err = Join-Path $LogDir "watch_$stamp.err.log"
    $args = @(
        "DouyinCSAgent\douyin_current_chat_reply.py",
        "--cdp", $Cdp,
        "--watch",
        "--no-platform-report",
        "-v"
    )
    if ($NoSend) {
        $args += "--no-send"
    } else {
        $args += "--send"
    }
    $p = Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $GuiRoot -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    Start-Sleep -Seconds 1
    $mode = if ($NoSend) { "dry-run" } else { "send" }
    Write-Output "started douyin watcher pid=$($p.Id) mode=$mode log=$err"
}

function Stop-Services {
    $targets = @()
    $targets += Get-WatcherProcess
    $targets += Get-LocalServerProcess
    foreach ($proc in $targets) {
        if ($proc.ProcessId) {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Output "stopped pid=$($proc.ProcessId)"
        }
    }
}

function Show-Status {
    $local = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" } |
        Select-Object -First 1
    if ($local) {
        Write-Output "local_carzone: running pid=$($local.OwningProcess) url=http://127.0.0.1:$Port"
    } else {
        Write-Output "local_carzone: stopped"
    }

    $watchers = @(Get-WatcherProcess)
    if ($watchers.Count -eq 0) {
        Write-Output "douyin watcher: stopped"
    } else {
        foreach ($watcher in $watchers) {
            $mode = if ($watcher.CommandLine -match "--send") { "send" } else { "dry-run" }
            Write-Output "douyin watcher: running pid=$($watcher.ProcessId) mode=$mode"
        }
    }
}

function Show-Logs {
    if (-not (Test-Path $LogDir)) {
        Write-Output "no log dir: $LogDir"
        return
    }
    Get-ChildItem $LogDir -Filter "*.log" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 6 Name, Length, LastWriteTime
    $latest = Get-ChildItem $LogDir -Filter "*.err.log" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($latest) {
        Write-Output ""
        Write-Output "tail $($latest.FullName)"
        Get-Content $latest.FullName -Tail 80
    }
}

switch ($Action) {
    "start" {
        Start-LocalServer
        Start-Watcher
        Show-Status
    }
    "stop" {
        Stop-Services
        Show-Status
    }
    "status" {
        Show-Status
    }
    "logs" {
        Show-Logs
    }
}
