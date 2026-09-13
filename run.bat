@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first. Python 3.11 or newer is required.
  exit /b 1
)
if "%~1"=="" (
  ".venv\Scripts\python.exe" bot.py preview
) else (
  ".venv\Scripts\python.exe" bot.py %*
)
exit /b %errorlevel%

