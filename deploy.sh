#!/usr/bin/env bash
# Linux / macOS: аренда сервера и развёртывание модели из консоли. Аналог deploy.bat.
set -euo pipefail
cd "$(dirname "$0")"

PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || { echo "Нужен Python 3.9 или новее"; exit 1; }

export PYTHONUTF8=1
echo "[deploy] Аренда GPU на Clore.ai и развёртывание модели из .env (MODEL_REPO)"
echo "Флаги: --model owner/name  --ctx 65536  --max-price 0.5  --order-type spot  --dry-run  --force"
exec "$PY" deploy.py "$@"
