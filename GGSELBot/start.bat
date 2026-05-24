@echo off
REM ============================================================
REM  GGSELBot — Windows launcher.
REM  Открывает командную строку, создаёт venv, ставит зависимости,
REM  запрашивает только Telegram Bot Token (если ещё не сохранён),
REM  запускает бота.
REM ============================================================

setlocal enableextensions enabledelayedexpansion
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
    set "PY=python"
) else (
    set "PY=py -3"
)

if not exist "venv\Scripts\python.exe" (
    echo [GGSELBot] Создаю venv...
    %PY% -m venv venv
    if errorlevel 1 (
        echo [GGSELBot] Не удалось создать venv. Установите Python 3.11+.
        pause
        exit /b 1
    )
)

echo [GGSELBot] Устанавливаю зависимости...
venv\Scripts\python.exe -m pip install --upgrade pip >nul
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [GGSELBot] pip install упал.
    pause
    exit /b 1
)

:loop
echo [GGSELBot] Запуск...
venv\Scripts\python.exe bot.py
echo [GGSELBot] Процесс завершился, перезапуск через 5 секунд...
timeout /t 5 /nobreak >nul
goto loop
