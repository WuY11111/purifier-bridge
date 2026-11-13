@echo off
chcp 65001 >nul
call "%~dp0_env.bat"
"%PY%" bridge.py once --dry-run
echo.
pause >nul
