@echo off
chcp 65001 >nul
title Purifier Bridge - running (close window to stop)
call "%~dp0_env.bat"
"%PY%" bridge.py run
echo.
pause >nul
