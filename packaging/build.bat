@echo off
setlocal
chcp 65001 >nul 2>&1
title Build SubtitleStudio
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
rem Bundle a locally downloaded model into the release, or leave empty.
rem Empty = users download the model in the app on first use.
set "BUNDLED_MODEL="
set "BUNDLED_LABEL=none (download in the app on first use)"
if defined BUNDLED_MODEL set "BUNDLED_LABEL=%BUNDLED_MODEL%"

echo ============================================================
echo   SubtitleStudio - build Windows exe (onedir)
echo   bundled model: %BUNDLED_LABEL%
echo ============================================================
echo.

if not exist "%PY%" (
    echo [!] .venv not found at .venv\Scripts\python.exe
    echo     Create it first:
    echo         python -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    goto end
)

echo [1/7] install / upgrade pyinstaller into .venv ...
"%PY%" -m pip install --upgrade --quiet pyinstaller
if errorlevel 1 goto failed

echo [2/7] make code frozen-safe (paths follow the exe) ...
"%PY%" "%~dp0make_frozen_safe.py"
if errorlevel 1 goto failed

echo [3/7] clean old output ...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "dist" (
    echo [!] could not delete dist - close SubtitleStudio.exe or Explorer windows first.
    goto failed
)

echo [4/7] run pyinstaller, this takes 1-8 minutes ...
"%PY%" -m PyInstaller --noconfirm --clean --distpath "dist" --workpath "build" "%~dp0app.spec"
if errorlevel 1 goto failed

if not exist "dist\SubtitleStudio\SubtitleStudio.exe" (
    echo [!] build finished but exe not found. Check the log above.
    goto failed
)

echo [5/7] create data folders next to the exe ...
mkdir "dist\SubtitleStudio\data\models"   2>nul
mkdir "dist\SubtitleStudio\data\uploads"  2>nul
mkdir "dist\SubtitleStudio\data\outputs"  2>nul

echo [6/7] bundle local model if configured ...
if defined BUNDLED_MODEL (
    "%PY%" "%~dp0bundle_models.py" --dist "dist\SubtitleStudio" --only "%BUNDLED_MODEL%"
    if errorlevel 1 goto failed
) else (
    echo       skipped - no model bundled, users download it in the app
)

echo [7/7] copy usage note ...
if exist "%~dp0README-usage.txt" copy /y "%~dp0README-usage.txt" "dist\SubtitleStudio\" >nul

echo.
echo ============================================================
echo   DONE: dist\SubtitleStudio\SubtitleStudio.exe
echo ============================================================
"%PY%" -c "import os;p='dist/SubtitleStudio';print('  size on disk:',round(sum(os.path.getsize(os.path.join(r,f)) for r,_,fs in os.walk(p) for f in fs)/1048576),'MB')"
echo.
echo   Next:
echo     1) double-click dist\SubtitleStudio\SubtitleStudio.exe
echo     2) optional installer: compile packaging\installer.iss with Inno Setup 6
echo.
echo   Notes:
echo     - upgrade overwrites program files, keeps data\ (models/uploads/outputs)
echo     - to bundle a model: set BUNDLED_MODEL=small above and rebuild, or run
echo         .venv\Scripts\python.exe packaging\bundle_models.py --dist dist\SubtitleStudio --only small
echo.
goto end

:failed
echo.
echo [BUILD FAILED] see the messages above.

:end
echo.
echo Press any key to close . . .
pause >nul
endlocal
