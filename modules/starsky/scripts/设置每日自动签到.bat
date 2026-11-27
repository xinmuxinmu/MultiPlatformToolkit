@echo off
chcp 65001 >nul
cd /d "%~dp0..\..\.."
set "SKY_TIME="
set /p "SKY_TIME=Daily time [09:30], or del to remove: "
if not defined SKY_TIME set "SKY_TIME=09:30"
set "SKY_ARGS=--schedule %SKY_TIME%"
if /i "%SKY_TIME%"=="del" set "SKY_ARGS=--remove-schedule"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "modules\starsky\scripts\checkin.py" %SKY_ARGS%
) else (
    python "modules\starsky\scripts\checkin.py" %SKY_ARGS%
)
pause
