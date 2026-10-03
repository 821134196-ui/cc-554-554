#!/usr/bin/env bash
# 一条命令启动：景区缆车停运退票与候补改签系统
set -e
cd "$(dirname "$0")"

VENV=.venv
if [ ! -x "$VENV/bin/python" ]; then
  echo "首次运行，创建虚拟环境..."
  python3 -m venv --without-pip "$VENV" 2>/dev/null || python3 -m venv "$VENV"
  if [ ! -x "$VENV/bin/pip" ]; then
    curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    "$VENV/bin/python" /tmp/get-pip.py -q
  fi
  "$VENV/bin/python" -m pip install -q -r requirements.txt
fi

PORT="${PORT:-8000}"
echo "启动服务：http://127.0.0.1:${PORT}  （游客页 / ，调度台 /dispatch.html ，管理台 /admin.html）"
exec "$VENV/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
