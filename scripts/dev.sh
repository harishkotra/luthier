#!/usr/bin/env bash
# Start the FastAPI/Strands agent on :3001 and the Vite client on :5173.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

SERVER_PORT="${PORT:-3001}"

"$PY" -m uvicorn agent.main:app --host 127.0.0.1 --port "$SERVER_PORT" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT INT TERM

for _ in $(seq 1 40); do
  if curl -s -o /dev/null "http://127.0.0.1:$SERVER_PORT/api/health"; then break; fi
  sleep 0.25
done

npm --prefix client run dev
