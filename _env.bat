@echo off
rem Resolve the Python interpreter once, used by the other .bat files.
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"
cd /d "%~dp0"
