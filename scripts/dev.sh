#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8000}"
export PAPER_DATA_DIR="${PAPER_DATA_DIR:-$ROOT_DIR/data}"
export ENABLE_OCR="${ENABLE_OCR:-false}"

if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="python3"
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  chmod 600 .env 2>/dev/null || true
  echo "[dev] Created .env from .env.example. Fill DEEPSEEK_API_KEY before real generation."
fi

echo "[dev] Reader: http://${APP_HOST}:${APP_PORT}/viewer/"
echo "[dev] Data:   ${PAPER_DATA_DIR}"
echo "[dev] Press Ctrl+C to stop."
echo

exec env PYTHONUNBUFFERED=1 "$PYTHON" -m uvicorn backend.server:app \
  --host "$APP_HOST" \
  --port "$APP_PORT" \
  --workers 1
