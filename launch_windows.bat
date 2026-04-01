@echo off
REM =============================================================================
REM Ticket Monitor — Windows Launcher
REM Double-click this file to open the Ticket Monitor app.
REM =============================================================================

cd /d "%~dp0"
title Ticket Monitor

if not exist "venv\" (
    echo Setup hasn't been run yet.
    echo Please double-click  setup_windows.bat  first.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat
python app.py

if errorlevel 1 (
    echo.
    echo ❌  The app exited with an error. See above for details.
    echo     If packages are missing, try running setup_windows.bat again.
    pause
)
