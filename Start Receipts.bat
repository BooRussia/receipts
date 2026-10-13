@echo off
setlocal
cd /d "%~dp0"
title Receipts

if not exist ".venv\Scripts\python.exe" (
    echo Setting up Receipts. First run only, this takes a few minutes...
    py -3.12 -m venv .venv 2>nul || python -m venv .venv || goto :failed
)

rem Install packages on first run, and again whenever an update changes requirements.txt.
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1 || (
    echo Installing packages...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :failed
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

start "" ".venv\Scripts\pythonw.exe" -m receipts %*
exit /b 0

:failed
echo.
echo Setup failed. Make sure Python 3.12 is installed from python.org
echo and that "Add python.exe to PATH" was ticked in the installer, then run this again.
pause
exit /b 1
