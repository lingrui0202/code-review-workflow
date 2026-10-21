@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo   代码审查工作流 · 网页版
echo   ------------------------------------
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo   [x] 没找到 python 命令。
  echo       请安装 Python 3.10+ 并勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

echo   正在启动本地服务，浏览器会自动打开 http://127.0.0.1:8765/
echo   用完后回到这个窗口按 Ctrl+C 停止。
echo.
python main.py web
echo.
echo   服务已停止。
pause
