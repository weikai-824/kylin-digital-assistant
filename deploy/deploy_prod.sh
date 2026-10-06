#!/bin/bash
# ============================================================
# 生产机部署脚本(由 Jenkins 远程调用,也可手动执行)
# 逻辑:拉代码 → 重建 app → 确保 oa-console → 健康检查
# 用法:bash deploy/deploy_prod.sh
# 说明:任何一步失败(非 0 退出)即终止,Jenkins 会标红
# ============================================================
set -euo pipefail

# 仓库根目录 = 本脚本所在目录的上一级(脚本在 deploy/ 下)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

echo "[1/4] git pull origin dev ..."
git pull origin dev

echo "[2/4] docker compose up -d --build app ..."
docker compose up -d --build app

echo "[3/4] docker compose up -d oa-console ..."
docker compose up -d oa-console

echo "[4/4] 健康检查 ..."
sleep 8

# 任一不通则以非 0 退出,让 Jenkins 构建标红
curl -fsS http://127.0.0.1:7861/health >/dev/null
echo "  - API 7861: OK"
curl -fsS -o /dev/null http://127.0.0.1:8501/
echo "  - WebUI 8501: OK"
curl -fsS -o /dev/null http://127.0.0.1:8502/
echo "  - oa-console 8502: OK"

echo "DEPLOY OK"
