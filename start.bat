@echo off
:: Delivery Zone Manager — Windows Launcher
:: Double-click this file to start the app.

title Delivery Zone Manager

echo ============================================
echo   Delivery Zone Manager
echo ============================================
echo.

cd /d "%~dp0"

:: Check Node.js
where node >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Node.js is not installed.
    echo.
    echo Please download and install Node.js from:
    echo   https://nodejs.org  ^(choose the LTS version^)
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('node -v') do set NODE_VERSION=%%v
echo Node.js: %NODE_VERSION%
echo.

:: Install dependencies if needed
if not exist "node_modules\" (
    echo Installing dependencies ^(first run only^)...
    npm install
    echo.
)

:: Kill any existing process on port 3000
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :3000 ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>&1
)

echo Starting server on http://localhost:3000 ...
echo Opening login page in your browser...
echo.
echo Close this window to stop the app.
echo.

:: Start server
start /b node server.js

:: Wait and open browser
timeout /t 2 /nobreak >nul
start http://localhost:3000/login

:: Keep window open to show logs
node server.js
