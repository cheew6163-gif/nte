@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" if exist ".venv\installed-v01.txt" exit /b 0
if exist ".venv\Scripts\python.exe" goto install
set "KACI_PY="
for %%V in (3.12 3.13 3.11) do (
    py -%%V -c "import struct; assert struct.calcsize('P') == 8" >nul 2>&1
    if not errorlevel 1 (
        set "KACI_PY=py -%%V"
        goto found
    )
)
python -c "import sys,struct; assert (3,11) <= sys.version_info[:2] < (3,14); assert struct.calcsize('P') == 8" >nul 2>&1
if not errorlevel 1 set "KACI_PY=python"
if not defined KACI_PY (
    echo Install 64-bit Python 3.12 from https://www.python.org/downloads/windows/
    echo Enable the Python launcher or Add Python to PATH, then run launch.bat again.
    exit /b 1
)
:found
%KACI_PY% -m venv .venv
if errorlevel 1 exit /b 1
:install
echo Installing dependencies. First launch needs Internet access.
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -c "import cv2,numpy,mss,PIL,dxcam"
if errorlevel 1 exit /b 1
echo ready>".venv\installed-v01.txt"
exit /b 0
