# 开发默认：叠 docker-compose.dev.yml（源码挂载 + KYLIN_DEV 热重载）
# 用法: .\scripts\dev.ps1 up|restart|rebuild|logs|down|ps|help
param(
    [Parameter(Position = 0)]
    [ValidateSet("help", "up", "restart", "rebuild", "logs", "down", "ps")]
    [string]$Cmd = "help"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$ComposeArgs = @("-p", "kylin-home", "-f", "docker-compose.yml", "-f", "docker-compose.dev.yml", "-f", "docker-compose.home.yml")

switch ($Cmd) {
    "help" {
        Write-Host ".\scripts\dev.ps1 up       - 启动/更新（叠 dev overlay）"
        Write-Host ".\scripts\dev.ps1 restart  - 秒级重启 app（不重装 pip）"
        Write-Host ".\scripts\dev.ps1 rebuild  - 同 up（仍禁止 --no-cache）"
        Write-Host ".\scripts\dev.ps1 logs     - 跟踪 app 日志"
        Write-Host ".\scripts\dev.ps1 down     - 停止栈"
        Write-Host ".\scripts\dev.ps1 ps       - 查看容器状态"
        Write-Host ""
        Write-Host "改 Python 代码：保存即可热重载，不要 restart / rebuild"
    }
    "up" { docker compose @ComposeArgs up -d --build }
    "rebuild" { docker compose @ComposeArgs up -d --build }
    "restart" { docker compose @ComposeArgs restart app }
    "logs" { docker compose @ComposeArgs logs -f app }
    "down" { docker compose @ComposeArgs down }
    "ps" { docker compose @ComposeArgs ps }
}
