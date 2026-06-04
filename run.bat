@echo off
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" (
    echo Python 3.12 not found at:
    echo   %PY%
    echo Install from https://www.python.org/downloads/ and check "Add python.exe to PATH".
    exit /b 1
)

cd /d "%~dp0"
"%PY%" -m pip install -r requirements.txt -q
"%PY%" bot.py %*
