@echo off
setlocal
chcp 65001 >nul 2>&1
title SubtitleStudio - Local Server
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "HF_ENDPOINT=https://hf-mirror.com"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"

rem ---------- 1. locate python (project venv first, then py / python) ----------
set PYCMD=
if exist "%~dp0.venv\Scripts\python.exe" set PYCMD="%~dp0.venv\Scripts\python.exe"
if not defined PYCMD ( where py >nul 2>&1 && set PYCMD=py -3 )
if not defined PYCMD ( where python >nul 2>&1 && set PYCMD=python )
if not defined PYCMD goto nopython
echo [1/3] Python: %PYCMD%

rem ---------- 2. 8765 已被占用时只打开旧服务，不热重载代码 ----------
netstat -ano -p tcp | findstr /c:"LISTENING" | findstr /c:"127.0.0.1:8765 " >nul 2>&1
if not errorlevel 1 goto running
echo [2/3] Port 8765 is free. Starting server...
echo ------------------------------------------------------------
%PYCMD% run.py
set "CODE=%ERRORLEVEL%"
echo ------------------------------------------------------------
if not "%CODE%"=="0" echo [!] Server exited with code %CODE% - see the error above.
goto end

:running
echo [2/3] 端口 8765 已被占用，正在打开已有的服务...
echo       注意：这里不会重新加载后端代码。
echo       如果刚更新过程序，请先关闭旧的启动窗口（或结束 python.exe），再运行本文件。
start "" "http://127.0.0.1:8765"
timeout /t 3 >nul 2>&1
goto end

:nopython
echo [!] Python not found and .venv is missing. Run these first:
echo         python -m venv .venv
echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
goto end

:end
echo.
echo Press any key to close this window . . .
pause >nul
endlocal
