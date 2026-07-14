@echo off
chcp 65001 >nul
set PORT=8080
echo.
echo === Ri Qi - Firewall Setup ===
echo.
echo 1. Finding Python...
for /f "skip=1 tokens=1,2 delims==" %%I in ('wmic process where "name='python.exe'" get executablepath /format:value 2^>nul') do set PYTHON_PATH=%%I
if "%PYTHON_PATH%"=="" set PYTHON_PATH=%USERPROFILE%\AppData\Local\Programs\Python\Python313\python.exe
echo    Path: %PYTHON_PATH%
echo.
echo 2. Adding firewall rule...
netsh advfirewall firewall add rule name="RiQi_%PORT%" dir=in action=allow program="%PYTHON_PATH%" protocol=tcp localport=%PORT%
echo.
if %errorlevel% equ 0 (
    echo [OK] Done! You can now run: python server.py
) else (
    echo [FAILED] Please right-click and select "Run as administrator"
)
echo.
pause
