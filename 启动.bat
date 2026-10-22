@echo off
chcp 65001 >nul
title 林离 · 书房
cd /d "%~dp0"

set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (python --version >nul 2>nul && set "PY=python")
if not defined PY (
  echo.
  echo   [X] 没有找到 Python
  echo.
  echo   这个工具需要 Python 3.10 以上版本。
  echo   现在帮你打开官网下载页，安装时记得勾选 "Add Python to PATH"。
  echo.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)

%PY% app.py
if errorlevel 1 (
  echo.
  echo   程序异常退出，把上面的报错截图发出来。
  pause
)
