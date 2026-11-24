@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "OUT=%~dp0..\AutoEdit-Share"
echo Making a clean copy for sharing (this can take a few minutes)...
if exist "%OUT%" rmdir /s /q "%OUT%"
robocopy "%~dp0." "%OUT%" /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ input output ".edit-*" ".review-*" "ซับที่ตรวจแล้ว" /XF account-map.json genre-map.json settings.json watch.lock stop.request Make-Share-Package.cmd *.zip
mkdir "%OUT%\input" "%OUT%\output" 2>nul
copy /y "%~dp0share-files\วิธีใช้.txt" "%OUT%\" >nul
copy /y "%~dp0share-files\ที่มาของเครื่องมือ.txt" "%OUT%\" >nul
rmdir /s /q "%OUT%\share-files" 2>nul
echo Zipping...
if exist "%~dp0..\AutoEdit-Share.zip" del "%~dp0..\AutoEdit-Share.zip"
tar -a -c -f "%~dp0..\AutoEdit-Share.zip" -C "%~dp0.." "AutoEdit-Share"
echo.
echo DONE. Upload this file to Google Drive:
echo %~dp0..\AutoEdit-Share.zip
explorer /select,"%~dp0..\AutoEdit-Share.zip"
pause
