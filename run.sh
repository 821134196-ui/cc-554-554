#!/usr/bin/env bash
# 一条命令启动：自动装依赖 → 初始化演示数据 → 启动服务
# 访问 http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

PY=python3
$PY -c "import fastapi, uvicorn, sqlalchemy, pydantic" 2>/dev/null || {
  echo "[run] 安装 Python 依赖 …"
  if [[ "$(uname -s)" == "Linux" ]] && grep -qE "ID=debian|ID=ubuntu|ID_LIKE=debian" /etc/os-release 2>/dev/null; then
    $PY -m pip install --break-system-packages -q -r requirements.txt || $PY -m pip install -q -r requirements.txt
  else
    $PY -m pip install -q -r requirements.txt
  fi
}

if [[ ! -f backend/cableway.db ]]; then
  echo "[run] 首次启动，初始化演示数据 …"
  $PY -m backend.seed
fi

echo "[run] 服务已启动： http://localhost:${PORT}"
exec $PY -m uvicorn backend.app:app --host 0.0.0.0 --port "${PORT}"
