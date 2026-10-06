#!/usr/bin/env bash
# 跑 data/eval 两套评估并快照基线到 data/eval/baseline/。
# 前置:MySQL / Milvus / ES / LLM 在线;KYLIN_ROOT 已配置。
# 用法:bash deploy/run_eval_baseline.sh [KB=oa_manuals]
set -euo pipefail
cd "$(dirname "$0")/.."

KB="${KB:-oa_manuals}"
BASELINE_DIR="data/eval/baseline"
mkdir -p "$BASELINE_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"

echo "== [1/2] 检索评估 (kb=$KB) =="
python -m kylin.adapters.inbound.jobs.eval.runner --kb "$KB"
cp data/eval/results/retrieval_eval.json "$BASELINE_DIR/${STAMP}_retrieval_eval.json"

echo "== [2/2] 回答评估 (kb=$KB) =="
python -m kylin.bootstrap.eval_answer --kb "$KB"
cp data/eval/results/answer_eval.json "$BASELINE_DIR/${STAMP}_answer_eval.json"

echo "== 基线已保存 =="
ls -l "$BASELINE_DIR/${STAMP}"_*.json
