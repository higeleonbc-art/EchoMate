@echo off
chcp 65001 >nul
title LoL ADC Coach
setlocal enabledelayedexpansion

cd /d "%~dp0"

if not exist ".env" (
    echo [ERROR] .env not found.
    echo Copy .env.example to .env and set RIOT_API_KEY.
    echo.
    pause
    exit /b 1
)

REM Quick sanity check that RIOT_API_KEY is not the placeholder
findstr /B "RIOT_API_KEY=RGAPI-" .env >nul
if errorlevel 1 (
    echo [WARN] RIOT_API_KEY may not be set properly in .env
    echo.
)

REM Check Ollama
curl -s -o nul -w "%%{http_code}" http://localhost:11434/api/tags > "%TEMP%\ollama_check.txt" 2>nul
set /p OLLAMA_STATUS=<"%TEMP%\ollama_check.txt"
del "%TEMP%\ollama_check.txt" >nul 2>nul
if not "!OLLAMA_STATUS!"=="200" (
    echo [WARN] Ollama not responding at http://localhost:11434
    echo Run 'ollama serve' if you want LLM coach comments.
    echo.
)

REM Launch GUI
python coach_gui.py
if errorlevel 1 (
    echo.
    echo [ERROR] coach_gui.py exited with error.
    echo If pywebview is missing run:  pip install -r requirements_coach.txt
    echo For CLI menu fallback run:    start_debug.bat
    echo.
    pause
)

endlocal
