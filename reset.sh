#!/usr/bin/env bash
# 清空数据并重新生成演示场景
set -euo pipefail
cd "$(dirname "$0")"
rm -f backend/cableway.db backend/cableway.db-wal backend/cableway.db-shm
python3 -m backend.seed
echo "[reset] 演示数据已重置，重新执行 ./run.sh 启动"
