@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PY=%~dp0.runtime\python-official\python.exe"
echo [1/4] Installing Thai word splitter...
"%PY%" -m pip install --quiet --disable-pip-version-check pythainlp
echo [2/4] Setting up folders (Google Drive if installed)...
"%PY%" "%~dp0auto_edit.py" --setup
echo [3/4] Keeping the computer awake when plugged in...
powercfg /change standby-timeout-ac 0 >nul 2>&1
echo [4/4] Starting automatically with Windows...
set "VBS=%TEMP%\autoedit-shortcut.vbs"
> "%VBS%" echo Set ws=CreateObject("WScript.Shell")
>> "%VBS%" echo Set s=ws.CreateShortcut(ws.SpecialFolders("Startup") ^& "\Auto-Edit.lnk")
>> "%VBS%" echo s.TargetPath="%~dp0Auto-Edit-Background.cmd"
>> "%VBS%" echo s.WorkingDirectory="%~dp0"
>> "%VBS%" echo s.WindowStyle=7
>> "%VBS%" echo s.Save
cscript //nologo "%VBS%"
del "%VBS%"
del ".runtime\stop.request" 2>nul
start "" /min "%~dp0Auto-Edit-Background.cmd"
echo.
echo ===== DONE ===== Put clips in the folder that just opened.
pause
