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
echo ⏳  Installing Chromium engine...
python -m playwright install chromium
if errorlevel 1 (
    echo ❌  Chromium install failed. Check your internet connection and try again.
    pause
    exit /b 1
)
echo ✅  Chromium installed.
echo.

REM ── Check for Google Chrome (used as the default browser channel) ─────────────
set "CHROME_FOUND=0"
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe"       set "CHROME_FOUND=1"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"         set "CHROME_FOUND=1"

if "%CHROME_FOUND%"=="1" (
    echo ✅  Google Chrome detected — registering with Playwright...
    python -m playwright install chrome
    echo ✅  Chrome channel ready.
) else (
    echo ⚠️   Google Chrome is NOT installed on this PC.
    echo.
    echo     The monitor works best with Chrome to avoid Ticketmaster bot-detection.
    echo     You can install it any time from: https://www.google.com/chrome/
    echo.
    echo     For now the app will automatically fall back to the bundled Chromium
    echo     that was just downloaded — login and monitoring will still work.
    echo.
    echo     After installing Chrome, run this setup file again to register it.
)
echo.
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
