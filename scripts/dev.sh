#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8787}"
FRONTEND_PORT="${FRONTEND_PORT:-8000}"

if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="python3"
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "[dev] Created .env from .env.example. Fill DEEPSEEK_API_KEY before real generation."
fi

cleanup() {
  echo
  echo "[dev] Stopping frontend/backend..."
  kill "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
  wait "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[dev] Backend:  http://${BACKEND_HOST}:${BACKEND_PORT}"
echo "[dev] Frontend: http://localhost:${FRONTEND_PORT}/viewer/"
echo "[dev] Logs from both processes will appear below. Press Ctrl+C to stop."
echo

PYTHONUNBUFFERED=1 "$PYTHON" -m uvicorn backend.server:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" &
BACKEND_PID=$!

"$PYTHON" -m http.server "$FRONTEND_PORT" &
FRONTEND_PID=$!

while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done

echo "[dev] One process exited; shutting down the other."
