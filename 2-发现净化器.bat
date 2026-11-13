@echo off
chcp 65001 >nul
call "%~dp0_env.bat"
"%PY%" bridge.py discover
echo.
pause >nul
