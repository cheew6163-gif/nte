@echo off
setlocal
cd /d "%~dp0"
call ensure_env.bat
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check "pyinstaller>=6.16,<7"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --onedir --name YiHuanVision --manifest app.manifest --add-data "assets;assets" --add-data "assets999;assets999" --add-data "config.json;." --collect-all dxcam --collect-submodules comtypes app.py
if errorlevel 1 goto failed
copy /Y config.json "dist\YiHuanVision\config.json" >nul
echo Built: dist\YiHuanVision\YiHuanVision.exe
echo Keep the entire YiHuanVision folder together when copying it.
pause
exit /b 0
:failed
echo Build failed. Read the message above.
pause
exit /b 1
