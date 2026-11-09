@echo off
rem Keep this file ASCII-only for the same reason as run.bat.
chcp 65001 >nul 2>nul
setlocal EnableExtensions
cd /d "%~dp0" || exit /b 1
title Subtitle Studio Installer Builder
set PYTHONUTF8=1

if not exist "%~dp0..\runtime\python\python.exe" (
    echo runtime\python\python.exe is missing.
    pause
    exit /b 1
)

"%~dp0..\runtime\python\python.exe" "%~dp0build_installer.py" %*
if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)
echo.
echo Build finished. Check the dist folder.
pause
