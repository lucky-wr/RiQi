@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0RiQi.exe" (
    start "" "%~dp0RiQi.exe" %*
    exit /b 0
)

where python >nul 2>nul
if errorlevel 1 (
    echo [Ri Qi] Python was not found in PATH.
    pause
    exit /b 1
)

python server.py %*
if errorlevel 1 pause
endlocal
