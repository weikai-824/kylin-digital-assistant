#!/bin/bash
set -euo pipefail

KYLIN_ROOT="${KYLIN_ROOT:-/data/kylin}"
MARKER="${KYLIN_ROOT}/.docker_bootstrapped"
EXAMPLE_DIR="/app/deploy/config.example"

mkdir -p "${KYLIN_ROOT}"
export KYLIN_ROOT
export PYTHONPATH="${PYTHONPATH:-/app}"
cd /app

wait_tcp() {
  local host="$1" port="$2" name="$3"
  echo "[entrypoint] waiting for ${name} at ${host}:${port} ..."
  for _ in $(seq 1 90); do
    if python - "$host" "$port" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(2)
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
except Exception:
    sys.exit(1)
finally:
    s.close()
sys.exit(0)
PY
    then
      echo "[entrypoint] ${name} is up"
      return 0
    fi
    sleep 2
  done
  echo "[entrypoint] timeout waiting for ${name}" >&2
  return 1
}

wait_tcp mysql 3306 mysql
wait_tcp elasticsearch 9200 elasticsearch
wait_tcp milvus 19530 milvus

if [[ ! -f "${MARKER}" ]]; then
  echo "[entrypoint] first boot: init dirs + DB tables + config"
  python -m kylin.cli init || true

  if [[ "${DOCKER_BOOTSTRAP_CONFIG:-1}" == "1" ]] && [[ -d "${EXAMPLE_DIR}" ]]; then
    echo "[entrypoint] apply docker config examples -> ${KYLIN_ROOT}"
    cp -f "${EXAMPLE_DIR}/"*.yaml "${KYLIN_ROOT}/" || true
  fi

  touch "${MARKER}"
fi

# 容器内始终把库地址写成服务名 mysql（本机 CLI 用 127.0.0.1 时，重启 app 会改回）
if [[ -n "${MYSQL_PASSWORD:-}" ]] && [[ -f "${KYLIN_ROOT}/basic_settings.yaml" ]]; then
  python - <<'PY'
import os, re
from pathlib import Path

root = Path(os.environ["KYLIN_ROOT"])
path = root / "basic_settings.yaml"
text = path.read_text(encoding="utf-8")
user = os.environ.get("MYSQL_USER", "kylin")
password = os.environ["MYSQL_PASSWORD"]
db = os.environ.get("MYSQL_DATABASE", "kylin_rag")
uri = f"mysql+pymysql://{user}:{password}@mysql:3306/{db}?charset=utf8mb4"
new = re.sub(
    r"(?m)^SQLALCHEMY_DATABASE_URI:.*$",
    f"SQLALCHEMY_DATABASE_URI: {uri}",
    text,
)
if new == text and "SQLALCHEMY_DATABASE_URI" not in text:
    new = text.rstrip() + f"\nSQLALCHEMY_DATABASE_URI: {uri}\n"
if new != text:
    path.write_text(new, encoding="utf-8")
    print("[entrypoint] patched SQLALCHEMY_DATABASE_URI")
PY
fi

# P1：把 .env（已通过 docker-compose env_file 注入容器）里的 DEEPSEEK_API_KEY
# 写到运行时 model_settings.yaml 的 deepseek 平台。
# 仅当 deepseek 平台存在且 api_key 为空时生效——保证：
#   1) 真 key 不进 git 仓库（仅在宿主机 .env）
#   2) 已手动写好的 yaml 不被覆盖；其他平台 api_key 不动
if [[ -n "${DEEPSEEK_API_KEY:-}" ]] && [[ -f "${KYLIN_ROOT}/model_settings.yaml" ]]; then
  python - <<'PY'
import os, re
from pathlib import Path

root = Path(os.environ["KYLIN_ROOT"])
path = root / "model_settings.yaml"
text = path.read_text(encoding="utf-8")
key = os.environ["DEEPSEEK_API_KEY"]

# 匹配 deepseek 平台区块内的 api_key 行（platform_name: deepseek 到下一个 platform_name 之前）
# 用 (?ms) 多行+dotall：只在该平台区块内做替换
new = re.sub(
    r"(?ms)(platform_name:\s*deepseek\b.*?api_key:\s*)\"[^\"]*\"",
    lambda m: m.group(1) + f'"{key}"',
    text,
    count=1,
)
if new != text:
    path.write_text(new, encoding="utf-8")
    print("[entrypoint] injected DEEPSEEK_API_KEY into deepseek platform")
else:
    # 兜底：区块内没找到 api_key 字段时直接追加（罕见，但兼容后续 yaml 改造）
    if "platform_name: deepseek" in text and "DEEPSEEK_API_KEY" not in os.environ.get("__PATCHED__", ""):
        print("[entrypoint] deepseek platform present but api_key line not found; skipped")
PY
fi

echo "[entrypoint] starting API + WebUI"
exec python -m kylin.cli start -a
