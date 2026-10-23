@echo off
setlocal
cd /d "%~dp0"
call ensure_env.bat
if errorlevel 1 (
    echo Setup failed. Read the message above and README.md.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
