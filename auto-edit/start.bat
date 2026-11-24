@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Auto Edit - running (do not close)
:loop
py -3.12 auto_edit.py
timeout /t 10 >nul
goto loop
