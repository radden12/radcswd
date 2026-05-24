@echo off
chcp 65001 >nul
title GGSEL Cardinal
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ================================================
echo   GGSEL Cardinal - запуск
echo ================================================

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python не найден в PATH.
    echo     Установите Python 3.11+ с https://python.org
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [+] Создаю виртуальное окружение .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [!] Не удалось создать .venv
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

echo [+] Обновляю pip ...
python -m pip install --upgrade pip >nul

echo [+] Устанавливаю зависимости ...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [!] Ошибка установки зависимостей
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [!] Создан .env из .env.example - откройте его и заполните токены.
        notepad .env
    )
)

if not exist "storage"  mkdir storage
if not exist "configs"  mkdir configs
if not exist "logs"     mkdir logs

:run
echo ================================================
echo   Запуск GGSEL Cardinal ...
echo ================================================
python main.py
set EXIT_CODE=%ERRORLEVEL%

if "%AUTO_RESTART%"=="0" goto end
if %EXIT_CODE% neq 0 (
    echo [!] Процесс завершился с кодом %EXIT_CODE%. Перезапуск через 5 сек...
    timeout /t 5 /nobreak >nul
    goto run
)

:end
echo Готово.
pause
