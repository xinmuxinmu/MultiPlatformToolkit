@echo off
chcp 65001 >nul
cd /d "%~dp0..\..\..\.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "src\modules\starsky\scripts\checkin.py"
) else (
    python "src\modules\starsky\scripts\checkin.py"
)
if errorlevel 1 pause
