@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    if exist "%~dp0RiQi.exe" (
        start "" "%~dp0RiQi.exe" %*
        exit /b 0
    )
    echo [Ri Qi] Neither server.py nor RiQi.exe is available.
    pause
    exit /b 1
)

python server.py %*
if errorlevel 1 pause
endlocal
