#!/usr/bin/env bash
# 无 Docker 环境下的本地裸跑启动（与 docker-compose 等价的两个进程）
# 可选：export COMPRESS_DELAY_MS=1000 给 gzip 压缩加人为延迟，复现并发压缩竞态
set -eu
cd "$(dirname "$0")/.."
export ORIGIN_BASE=http://127.0.0.1:18080
export COMPRESS_DELAY_MS="${COMPRESS_DELAY_MS:-0}"
mkdir -p .run
.venv/bin/python -m uvicorn app.origin:app --host 127.0.0.1 --port 18080 \
  > .run/origin.log 2>&1 &
echo $! > .run/origin.pid
COMPRESS_DELAY_MS="$COMPRESS_DELAY_MS" .venv/bin/python -m uvicorn app.proxy:app --host 127.0.0.1 --port 18000 \
  > .run/proxy.log 2>&1 &
echo $! > .run/proxy.pid
echo "origin  -> http://127.0.0.1:18080 (pid $(cat .run/origin.pid))"
echo "proxy   -> http://127.0.0.1:18000 (pid $(cat .run/proxy.pid))"
