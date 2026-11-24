@echo off
chcp 65001 >nul
echo Installing Thai word splitter...
"%~dp0.runtime\python-official\python.exe" -m pip install --quiet pythainlp
set "VBS=%TEMP%\autoedit-shortcut.vbs"
> "%VBS%" echo Set ws=CreateObject("WScript.Shell")
>> "%VBS%" echo Set s=ws.CreateShortcut(ws.SpecialFolders("Startup") ^& "\Auto-Edit.lnk")
>> "%VBS%" echo s.TargetPath="%~dp0Auto-Edit-Background.cmd"
>> "%VBS%" echo s.WorkingDirectory="%~dp0"
>> "%VBS%" echo s.WindowStyle=7
>> "%VBS%" echo s.Save
cscript //nologo "%VBS%"
del "%VBS%"
echo Done. Auto Edit will start by itself every time the computer turns on.
start "" "%~dp0Auto-Edit-Background.cmd"
pause
