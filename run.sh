#!/usr/bin/env bash
# GGSEL Cardinal — Linux/macOS launcher
set -euo pipefail

cd "$(dirname "$0")"

echo "================================================"
echo "  GGSEL Cardinal — запуск"
echo "================================================"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[!] python3 не найден. Установите Python 3.11+."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "[+] Создаю виртуальное окружение .venv ..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[+] Обновляю pip ..."
python -m pip install --upgrade pip >/dev/null

echo "[+] Устанавливаю зависимости ..."
python -m pip install -r requirements.txt

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "[!] Создан .env — откройте его и заполните токены, затем запустите снова."
    exit 0
fi

mkdir -p storage configs logs

AUTO_RESTART=${AUTO_RESTART:-1}

while true; do
    echo "================================================"
    echo "  Запуск GGSEL Cardinal ..."
    echo "================================================"
    set +e
    python main.py
    code=$?
    set -e

    if [ "$AUTO_RESTART" != "1" ] || [ "$code" -eq 0 ]; then
        exit "$code"
    fi
    echo "[!] Процесс завершился с кодом $code. Перезапуск через 5 сек..."
    sleep 5
done
