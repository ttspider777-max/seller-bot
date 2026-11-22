@echo off
cd /d "%~dp0"
if not exist .env (
    echo Copy .env.example to .env and fill in BOT_TOKEN and ADMIN_ID.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    pause
    exit /b 1
)
".venv\Scripts\python.exe" main.py
pause
