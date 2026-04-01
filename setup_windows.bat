@echo off
REM =============================================================================
REM Ticket Monitor — Windows Setup Script
REM Double-click this file to set up the monitor for the first time.
REM =============================================================================

cd /d "%~dp0"
title Ticket Monitor — Setup

echo.
echo ╔══════════════════════════════════════════╗
echo ║         Ticket Monitor — Setup           ║
echo ╚══════════════════════════════════════════╝
echo.

REM ── Check for Python 3 ───────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌  Python is not installed or not found.
    echo.
    echo Please download and install Python 3 from:
    echo   https://www.python.org/downloads/
    echo.
    echo IMPORTANT: During install, check "Add Python to PATH"
    echo.
    echo After installing Python, double-click this setup file again.
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo ✅  Found %%i
echo.

REM ── Create virtual environment ────────────────────────────────────────────────
if not exist "venv\" (
    echo ⏳  Creating virtual environment...
    python -m venv venv
    echo ✅  Virtual environment created.
) else (
    echo ✅  Virtual environment already exists.
)
echo.

REM ── Install packages ─────────────────────────────────────────────────────────
echo ⏳  Installing Python packages (this may take a minute)...
call venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

if errorlevel 1 (
    echo ❌  Package installation failed. Check your internet connection and try again.
    pause
    exit /b 1
)
echo ✅  Python packages installed.
echo.

REM ── Install Playwright browser ────────────────────────────────────────────────
echo ⏳  Installing browser engine (Chromium)...
python -m playwright install chromium
if errorlevel 1 (
    echo ⚠️   Playwright browser install had issues. The monitor may still work if Chrome is installed.
)
echo ✅  Browser engine ready.
echo.

REM ── Done ──────────────────────────────────────────────────────────────────────
echo ╔══════════════════════════════════════════╗
echo ║           Setup Complete! 🎉             ║
echo ╚══════════════════════════════════════════╝
echo.
echo Next steps:
echo   1. Double-click  launch_windows.bat  to open the app
echo   2. Add your concert URL in the Events tab
echo   3. Set your preferences and Discord webhook
echo   4. Log in to Ticketmaster in the Login tab
echo   5. Hit Start Monitor — and relax!
echo.
pause
