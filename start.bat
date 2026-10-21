@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo   代码审查与修复工作流 · 本地演示
echo   ------------------------------------
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo   [x] 没找到 python 命令。
  echo       请安装 Python 3.10+ 并勾选 "Add Python to PATH"，或改用命令行运行。
  echo.
  pause
  exit /b 1
)

echo   [1/2] 跑一遍离线演示（不需要 API Key）...
echo.
python main.py run --offline
if errorlevel 1 (
  echo.
  echo   [x] 运行失败，把上面的报错发我即可。
  echo.
  pause
  exit /b 1
)

echo.
echo   [2/2] 打开报告 ...
set "LATEST="
for /f "delims=" %%d in ('dir /b /ad /o-d "runs" 2^>nul') do (
  if not defined LATEST set "LATEST=runs\%%d"
)

if not defined LATEST (
  echo   [x] 没找到 runs 目录，请把上面的输出发我。
  echo.
  pause
  exit /b 1
)

echo       %CD%\%LATEST%\report.html
if not "%NO_OPEN%"=="1" (
  start "" "%CD%\%LATEST%\report.html"
  start "" "%CD%\%LATEST%"
)
echo.
pause
