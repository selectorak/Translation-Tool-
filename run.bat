@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Starting translator...
echo Python:
where python 2>nul
echo.

python translator.py
if %errorlevel% equ 0 exit

python3 translator.py
if %errorlevel% equ 0 exit

echo.
echo ============================================
echo  Start failed. Please install Python 3:
echo    https://www.python.org/downloads/
echo  Then run: pip install -r requirements.txt
echo ============================================
echo.
pause
