@echo off
rem Release template. The packager places this file at the portable ZIP root.
rem Keep this file ASCII-only. cmd may read it with the OEM code page when
rem chcp 65001 is unavailable, and non-ASCII text can corrupt parsing.
chcp 65001 >nul 2>nul
setlocal EnableExtensions
cd /d "%~dp0" || exit /b 1
title Subtitle Studio
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
rem Keep everything inside this folder: never use user site-packages.
set PYTHONNOUSERSITE=1
set PIP_DISABLE_PIP_VERSION_CHECK=1
if not exist "data\cache\pip" mkdir "data\cache\pip"
if not exist "data\logs" mkdir "data\logs"
set "PIP_CACHE_DIR=%CD%\data\cache\pip"

rem Portable runtime shipped with this package; no system Python needed.
set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" goto nopython

rem All checks, dependency repair and launch live in one Python entry point so
rem the console can show readable Chinese steps and a persistent progress bar.
"%PY%" "tools\bootstrap.py" %*
if errorlevel 1 goto failed
exit /b 0

:nopython
echo runtime\python\python.exe is missing. Extract the whole archive again.
pause
exit /b 1

:failed
echo.
echo Setup did not finish. Check data\logs\bootstrap.log and data\logs\pip.log.
pause
exit /b 1
