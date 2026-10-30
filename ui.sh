#!/usr/bin/env bash
# Linux / macOS: запуск веб-интерфейса. Аналог ui.bat для Windows.
set -euo pipefail
cd "$(dirname "$0")"

# В большинстве дистрибутивов и в macOS команда `python` отсутствует или ведёт на Python 2
PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || { echo "Нужен Python 3.9 или новее: https://www.python.org/downloads/"; exit 1; }
"$PY" - <<'CHECK' || exit 1
import sys
if sys.version_info < (3, 9):
    sys.exit(f"Нужен Python 3.9+, установлен {sys.version.split()[0]}")
CHECK

export PYTHONUTF8=1
echo "[ui] UncensoredAI: выбор модели с Hugging Face + аренда GPU на Clore.ai"
echo "Откроется http://127.0.0.1:8765 — Ctrl+C чтобы остановить."
exec "$PY" app.py "$@"
