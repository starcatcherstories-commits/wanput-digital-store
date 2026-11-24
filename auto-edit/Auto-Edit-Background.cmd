@echo off
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
title Auto Edit - running
:loop
".runtime\python-official\python.exe" "%~dp0auto_edit.py"
if exist ".runtime\stop.request" goto end
timeout /t 15 >nul
goto loop
:end
