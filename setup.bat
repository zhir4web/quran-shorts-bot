@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
where py >nul 2>nul
if errorlevel 1 (
  python -m venv .venv
) else (
  py -3 -m venv .venv
)
if errorlevel 1 exit /b 1
:install
".venv\Scripts\python.exe" -m ensurepip --upgrade
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
if errorlevel 1 exit /b 1
if not exist "queue.json" echo {"items": []}>"queue.json"
".venv\Scripts\python.exe" bot.py doctor
pause
