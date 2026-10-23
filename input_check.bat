@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
set "YIHUAN_CHECK_LOG=%~dp0logs\input_check_launcher.log"
>"%YIHUAN_CHECK_LOG%" echo [%date% %time%] Input check launcher
if defined YIHUAN_PYTHON goto interpreter_ready
set "YIHUAN_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%YIHUAN_PYTHON%" goto interpreter_ready
call ensure_env.bat >>"%YIHUAN_CHECK_LOG%" 2>&1
if errorlevel 1 goto setup_failed
:interpreter_ready
>>"%YIHUAN_CHECK_LOG%" echo Python: %YIHUAN_PYTHON%
"%YIHUAN_PYTHON%" -X utf8 -c "import cv2, numpy, mss; import tkinter" >>"%YIHUAN_CHECK_LOG%" 2>&1
if errorlevel 1 goto interpreter_failed
echo.
echo 请先关闭旧助手和Lua宏。当前游戏为管理员权限时，请以管理员身份运行本检查。
echo 1. 只读环境报告
echo 2. 桌面靶窗：只移动
echo 3. 桌面靶窗：单击一次
echo 4. 游戏前往：只移动
echo 5. 游戏前往：单击一次并观察匹配页
echo.
choice /c 12345 /n /m "选择检查（左 Ctrl 停止）："
if errorlevel 5 goto game_click
if errorlevel 4 goto game_move
if errorlevel 3 goto desktop_click
if errorlevel 2 goto desktop_move
>>"%YIHUAN_CHECK_LOG%" echo Mode: inspect
"%YIHUAN_PYTHON%" -X utf8 app.py --input-check inspect >>"%YIHUAN_CHECK_LOG%" 2>&1
goto finished
:desktop_move
>>"%YIHUAN_CHECK_LOG%" echo Mode: desktop move-only
"%YIHUAN_PYTHON%" -X utf8 app.py --input-check desktop --move-only >>"%YIHUAN_CHECK_LOG%" 2>&1
goto finished
:desktop_click
>>"%YIHUAN_CHECK_LOG%" echo Mode: desktop click
"%YIHUAN_PYTHON%" -X utf8 app.py --input-check desktop --click >>"%YIHUAN_CHECK_LOG%" 2>&1
goto finished
:game_move
>>"%YIHUAN_CHECK_LOG%" echo Mode: game go move-only
"%YIHUAN_PYTHON%" -X utf8 app.py --input-check game --move-only --target go >>"%YIHUAN_CHECK_LOG%" 2>&1
goto finished
:game_click
>>"%YIHUAN_CHECK_LOG%" echo Mode: game go click
"%YIHUAN_PYTHON%" -X utf8 app.py --input-check game --click --target go --timeout 20 >>"%YIHUAN_CHECK_LOG%" 2>&1
goto finished
:interpreter_failed
>>"%YIHUAN_CHECK_LOG%" echo Python interpreter could not start or dependencies are missing.
echo Python解释器无法启动或缺少依赖：%YIHUAN_PYTHON%
echo 可设置 YIHUAN_PYTHON 为已安装依赖的Python完整路径，然后重试。
goto setup_failed
:finished
set "YIHUAN_CHECK_STATUS=%errorlevel%"
type "%YIHUAN_CHECK_LOG%"
echo.
echo 本次结果见上方JSON和diagnostics中的input_check目录。
echo 启动日志：%YIHUAN_CHECK_LOG%
pause
exit /b %YIHUAN_CHECK_STATUS%
:setup_failed
type "%YIHUAN_CHECK_LOG%"
echo 启动日志已保留：%YIHUAN_CHECK_LOG%
pause
exit /b 1
