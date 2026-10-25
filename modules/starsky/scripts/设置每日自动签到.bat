@echo off
title 设置 StarSky 每日自动签到
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo   设置 StarSky 每日自动签到
echo   ----------------------------------------
echo   输入每天的签到时间（24 小时制，例如 09:30）
echo   直接回车 = 使用默认 09:30
echo   输入 del  = 删除已有的定时任务
echo.
set "TT="
set /p TT=请输入: 

if /i "%TT%"=="del" goto deltask
if "%TT%"=="" set "TT=09:30"

schtasks /Create /TN "StarSky自动签到" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%~dp0StarSky签到.ps1\" -Quiet" /SC DAILY /ST %TT% /RL HIGHEST /F
if errorlevel 1 (
    echo.
    echo 创建失败，请检查时间格式（例如 09:30）。
) else (
    echo.
    echo 已创建：每天 %TT% 自动签到（以最高权限运行，不会弹 UAC 提示）
)
echo.
pause
exit /b

:deltask
schtasks /Delete /TN "StarSky自动签到" /F
echo.
pause
exit /b
