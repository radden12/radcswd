#!/usr/bin/env bash
# GGSELBot — Linux/macOS launcher
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "Установите Python 3.11+"; exit 1; }

if [ ! -d venv ]; then
    echo "[GGSELBot] Создаю venv..."
    "$PY" -m venv venv
fi

echo "[GGSELBot] Зависимости..."
./venv/bin/python -m pip install --upgrade pip >/dev/null
./venv/bin/python -m pip install -r requirements.txt

while true; do
    echo "[GGSELBot] Запуск..."
    ./venv/bin/python bot.py || true
    echo "[GGSELBot] упал — рестарт через 5 секунд"
    sleep 5
done
