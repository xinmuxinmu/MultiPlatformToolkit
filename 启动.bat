@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating virtual environment...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

set "PORT=%APP_PORT%"
if not defined PORT set "PORT=18000"

rem Quiet by default: the console stays blank, logs go to data\logs\app.log.
rem Set APP_VERBOSE=1 before running to see the output in the console again.
if not defined APP_VERBOSE set "APP_QUIET=1"

rem If the port is already taken, offer to stop the old instance first.
set "OLDPID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do set "OLDPID=%%P"
if not defined OLDPID goto run

echo.
echo The app is already running: http://127.0.0.1:%PORT%/
echo If the page still shows an old version, stop the old instance and start again.
choice /c YN /n /m "Stop the old instance and restart? [Y/N] "
if errorlevel 2 goto open_existing

rem Only stop the process if it is really this app (command line contains main.py).
powershell -NoProfile -Command "$p=Get-CimInstance Win32_Process -Filter 'ProcessId=%OLDPID%'; if ($p -and $p.CommandLine -match 'main\.py') { Stop-Process -Id %OLDPID% -Force; exit 0 } exit 1"
if errorlevel 1 goto wrong_process
timeout /t 2 >nul
goto run

:wrong_process
echo.
echo Port %PORT% is used by another program. Startup cancelled.
pause
exit /b 1

:open_existing
start "" "http://127.0.0.1:%PORT%/"
exit /b 0

:run
rem Run Python in its own console as a plain command (not a batch job),
rem so Ctrl+C never triggers "Terminate batch job (Y/N)?".
start "Multi-Platform Tools" cmd /c ".venv\Scripts\python.exe main.py & if errorlevel 1 pause"
exit /b 0
