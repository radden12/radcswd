#!/usr/bin/env bash
# GGSELCardinal — Linux/macOS launcher (one-click)
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "Установите Python 3.11+"; exit 1; }

if [ ! -d venv ]; then
    echo "[GGSELCardinal] Создаю venv..."
    "$PY" -m venv venv
fi

echo "[GGSELCardinal] Зависимости..."
./venv/bin/python -m pip install --upgrade pip >/dev/null
./venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    echo "[GGSELCardinal] Создан .env — отредактируйте его и запустите снова."
    exit 0
fi

while true; do
    echo "[GGSELCardinal] Запуск..."
    ./venv/bin/python main.py || true
    echo "[GGSELCardinal] упал — рестарт через 5 секунд"
    sleep 5
done
