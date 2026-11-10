@echo off
chcp 65001 >nul
title TradingAgents Workbench
cd /d "%~dp0.."

echo ================================================
echo   TradingAgents Multi-Agent Workbench
echo ================================================
echo.

REM Locate usable Python
set "PY_CMD="
py -3 --version >nul 2>nul && set "PY_CMD=py -3"
if not defined PY_CMD (
    "C:\Users\GAOSONG\AppData\Local\Programs\Python\Python312\python.exe" --version >nul 2>nul && set "PY_CMD=C:\Users\GAOSONG\AppData\Local\Programs\Python\Python312\python.exe"
)
if not defined PY_CMD (
    "C:\Users\GAOSONG\.workbuddy\binaries\python\versions\3.13.12\python.exe" --version >nul 2>nul && set "PY_CMD=C:\Users\GAOSONG\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
if not defined PY_CMD (
    echo [ERROR] Python not found. Please install Python 3.10+.
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Using Python: %PY_CMD%

REM Check Docker
where docker >nul 2>nul
if errorlevel 1 (
    echo [INFO] Docker not found. Analysis will be unavailable, but you can still browse reports.
) else (
    docker info >nul 2>nul
    if errorlevel 1 (
        echo [INFO] Docker engine not running. You can browse reports now; start Docker before running analysis.
    ) else (
        echo [OK] Docker engine is running.
    )
)

echo [1/2] Starting local workbench server...

netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>nul
if errorlevel 1 (
    start "TradingAgents-Workbench" /min cmd /c "%PY_CMD% webapp\server.py"
    echo [2/2] Waiting for server to start...
    timeout /t 3 /nobreak >nul
) else (
    echo [2/2] Server already running, opening page...
)

start "" "http://127.0.0.1:8765"

echo.
echo Workbench started. Browser opened: http://127.0.0.1:8765
echo To stop: close the "TradingAgents-Workbench" console window.
echo.
echo NOTE: Turn on VPN before running analysis (data sources need Yahoo/Reddit).
echo.
pause
