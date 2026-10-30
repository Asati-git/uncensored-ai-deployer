#!/usr/bin/env bash
# Linux / macOS: отмена заказа Clore. Аналог stop.bat.
set -euo pipefail
cd "$(dirname "$0")"

PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || { echo "Нужен Python 3.9 или новее"; exit 1; }

export PYTHONUTF8=1
echo "Заказ Clore будет отменён: сервер освободится, скачанная модель удалится."
read -r -p "Продолжить? [y/N] " a
[ "${a:-n}" = "y" ] || [ "${a:-n}" = "Y" ] || { echo "отменено"; exit 0; }
exec "$PY" stop.py "$@"
