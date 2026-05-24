@echo off
REM ============================================================
REM  GGSELCardinal — Windows launcher (one-click)
REM  Создаёт venv, ставит зависимости, запускает main.py.
REM  При падении — авто-перезапуск через 5 секунд.
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
    echo [GGSELCardinal] Создаю виртуальное окружение...
    %PY% -m venv venv
    if errorlevel 1 (
        echo [GGSELCardinal] Не удалось создать venv. Установите Python 3.11+.
        pause
        exit /b 1
    )
)

echo [GGSELCardinal] Устанавливаю зависимости...
venv\Scripts\python.exe -m pip install --upgrade pip >nul
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [GGSELCardinal] pip install упал. См. вывод выше.
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [GGSELCardinal] Создан .env из .env.example — заполните токены!
        notepad .env
    )
)

:loop
echo [GGSELCardinal] Старт GGSELCardinal...
venv\Scripts\python.exe main.py
echo [GGSELCardinal] Процесс завершился, перезапуск через 5 секунд...
timeout /t 5 /nobreak >nul
goto loop
