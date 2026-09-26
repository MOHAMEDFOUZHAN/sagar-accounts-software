@echo off
title Sagar Accounts Software - Unified Financial Engine
echo ========================================================
echo        SAGAR ACCOUNTS SOFTWARE - FINANCIAL ENGINE
echo ========================================================
echo.
echo Checking environment...

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH!
    pause
    exit /b
)

echo Initializing database schemas (if needed)...
python init_db.py

echo Starting Accounts Web Service on port 5050...
start "" http://127.0.0.1:5050
python app.py

pause
