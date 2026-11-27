@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem Prefer this project's usable interpreter, then the Python launcher, then PATH.
if not exist ".venv\Scripts\python.exe" goto try_py
".venv\Scripts\python.exe" -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 goto try_py
".venv\Scripts\python.exe" -X utf8 "%~dp0src\bootstrap.py" %*
set "TOOLKIT_EXIT=%errorlevel%"
goto finish

:try_py
py -3 -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 goto try_python
py -3 -X utf8 "%~dp0src\bootstrap.py" %*
set "TOOLKIT_EXIT=%errorlevel%"
goto finish

:try_python
python -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 goto no_python
python -X utf8 "%~dp0src\bootstrap.py" %*
set "TOOLKIT_EXIT=%errorlevel%"
goto finish

:no_python
echo 未找到可用的 Python 3.10 或更新版本。
echo 请安装完整 Python 并启用 Python Launcher 或勾选 Add Python to PATH，再重新运行。
set "TOOLKIT_EXIT=9009"

:finish
if "%TOOLKIT_EXIT%"=="0" exit /b 0
echo.
echo 启动失败，退出码 %TOOLKIT_EXIT%。请查看上方提示或 data\logs\app.log。
pause
exit /b %TOOLKIT_EXIT%
