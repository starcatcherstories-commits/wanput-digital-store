@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
".runtime\python-official\python.exe" "%~dp0auto_edit.py" %*
if errorlevel 1 pause
