@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==========================================
echo   Olivia Lin Music Library - updater
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python not found in PATH.
  pause
  exit /b 1
)

python export.py %*
set RC=%errorlevel%
echo.
if %RC% neq 0 (
  echo [X] Failed with code %RC%
) else (
  echo [OK] Done. Double-click the html to listen.
)
pause
