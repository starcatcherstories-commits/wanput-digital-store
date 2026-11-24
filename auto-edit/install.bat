@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Installing Auto Edit (first time only, about 10-20 min) ===
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
py -3.12 -m pip install --upgrade pip
py -3.12 -m pip install faster-whisper pythainlp
echo Set ws=CreateObject("WScript.Shell"):Set s=ws.CreateShortcut(ws.SpecialFolders("Startup") ^& "\AutoEdit.lnk"):s.TargetPath="%~dp0start.bat":s.WindowStyle=7:s.WorkingDirectory="%~dp0":s.Save > "%TEMP%\mk.vbs"
cscript //nologo "%TEMP%\mk.vbs"
echo.
echo === DONE === Please restart the computer once.
pause
