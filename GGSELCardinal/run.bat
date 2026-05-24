@echo off
REM Запуск без авто-перезапуска (для отладки).
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
    echo [GGSELCardinal] venv не найден. Сначала запустите start.bat.
    pause
    exit /b 1
)
venv\Scripts\python.exe main.py
pause
