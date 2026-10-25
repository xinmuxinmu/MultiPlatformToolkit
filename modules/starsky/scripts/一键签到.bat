@echo off
title StarSky 自动签到
cd /d "%~dp0"

rem StarSky 是管理员权限运行的，本工具也必须提权，否则点不动按钮
net session >nul 2>&1
if errorlevel 1 (
    echo 需要管理员权限，正在申请提权 ...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0StarSky签到.ps1"
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" (
    echo 签到没有成功，请把上面的提示发给作者排查。
    pause
) else (
    timeout /t 2 >nul
)
exit /b %RC%
