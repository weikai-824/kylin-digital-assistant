# 一键回归评估：检索评估 -> 回答评估(默认 LLM 裁判) -> 入库审计 -> 基线对比报告
# 在 app 容器内执行（dev overlay 已挂载源码，结果经挂载落回宿主机 data/eval/results/）
# 前置：compose 栈在线（MySQL / Milvus / ES / LLM），否则先 .\scripts\dev.ps1 up
#
# 用法:
#   .\scripts\eval.ps1                                  # 全量四步跑（kb=oa_ma）
#   .\scripts\eval.ps1 -KB oa_ma -Limit 5               # 回答评估只跑前 5 题试点
#   .\scripts\eval.ps1 -NoJudge                         # 回答评估不开 LLM 裁判（更快）
#   .\scripts\eval.ps1 -SkipRetrieval                   # 只重跑回答评估 + 审计 + 对比
#   .\scripts\eval.ps1 -SkipAnswer                      # 只重跑检索评估 + 审计 + 对比
#   .\scripts\eval.ps1 -SkipIngestion                   # 跳过入库审计
#
# 退出码：0 = 无回归；1 = 存在回归（compare 判定）；2 = 环境问题（容器未运行 / 评估跑挂）
# 注意：本脚本只做"本轮 vs 基线"回归对比，不落基线快照；快照用 bash deploy/run_eval_baseline.sh
param(
    [string]$KB = "oa_ma",
    [int]$Limit = 0,
    [switch]$NoJudge,
    [switch]$SkipRetrieval,
    [switch]$SkipAnswer,
    [switch]$SkipIngestion
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$ComposeArgs = @("-f", "docker-compose.yml", "-f", "docker-compose.dev.yml")

# ---- 前置检查：app 容器须在运行 ----
$running = docker compose @ComposeArgs ps --status running --services 2>$null
if ($running -notcontains "app") {
    Write-Host "app 容器未运行，先执行 .\scripts\dev.ps1 up" -ForegroundColor Red
    exit 2
}

$step = 0
function Invoke-EvalStep([string]$Title, [string[]]$PyArgs, [switch]$AllowRegression) {
    $script:step++
    Write-Host ""
    Write-Host "== [$script:step] $Title ==" -ForegroundColor Cyan
    docker compose @ComposeArgs exec -T app python @PyArgs
    if ($LASTEXITCODE -ne 0) {
        if ($AllowRegression) {
            # compare 的 exit 1 = 正常跑完但判定回归，不是失败：原样返回，由末尾汇总
            return
        }
        Write-Host "$Title 失败（exit $LASTEXITCODE），中止后续步骤" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

# ---- [1] 检索评估 ----
if (-not $SkipRetrieval) {
    Invoke-EvalStep "检索评估 (kb=$KB)" @(
        "-m", "kylin.adapters.inbound.jobs.eval.runner", "--kb", $KB
    )
}

# ---- [2] 回答评估 ----
if (-not $SkipAnswer) {
    $answerArgs = @("-m", "kylin.bootstrap.eval_answer", "--kb", $KB)
    if (-not $NoJudge) { $answerArgs += "--judge" }
    if ($Limit -gt 0) { $answerArgs += @("--limit", "$Limit") }
    Invoke-EvalStep "回答评估 (kb=$KB, judge=$(-not $NoJudge)$(if ($Limit -gt 0) { ", limit=$Limit" }))" $answerArgs
}

# ---- [3] 入库审计（轻量间接，无 LLM；产物并入 compare 报告）----
if (-not $SkipIngestion) {
    Invoke-EvalStep "入库审计 (kb=$KB)" @(
        "-m", "kylin.adapters.inbound.jobs.eval.ingestion_audit", "--kb", $KB
    )
}

# ---- [4] 基线对比 ----
if ($SkipRetrieval -and $SkipAnswer) {
    if ($SkipIngestion) {
        Write-Host "检索 / 回答 / 审计均跳过，无需对比" -ForegroundColor Yellow
    } else {
        Write-Host "检索与回答均跳过：审计产物已落盘（ingestion_audit.json），但对比报告需至少一侧 runner 结果，跳过 compare" -ForegroundColor Yellow
    }
    exit 0
}
Invoke-EvalStep "基线对比 (compare)" @(
    "-m", "kylin.adapters.inbound.jobs.eval.compare"
) -AllowRegression

# compare: 0=无回归 1=有回归（exec 透传其退出码）
Write-Host ""
if ($LASTEXITCODE -eq 0) {
    Write-Host "✅ 无回归，报告: data\eval\results\compare_report.md" -ForegroundColor Green
} else {
    Write-Host "⚠ 存在回归（exit $LASTEXITCODE），详见 data\eval\results\compare_report.md" -ForegroundColor Red
}
exit $LASTEXITCODE
