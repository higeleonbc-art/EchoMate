@echo off
chcp 65001 >nul
title EchoMate

echo ============================================================
echo   EchoMate - Game AI Companion
echo ============================================================
echo.

REM ── 1. VOICEVOX チェック ─────────────────────────────────────
curl -s http://localhost:50021/version >nul 2>&1
if %errorlevel% == 0 (
    echo [OK] VOICEVOX is running
) else (
    echo [..] VOICEVOX not detected, trying to launch...

    REM よくあるインストール先を順番に探す
    set "VOICEVOX_EXE="
    if exist "%LOCALAPPDATA%\Programs\VOICEVOX\VOICEVOX.exe" (
        set "VOICEVOX_EXE=%LOCALAPPDATA%\Programs\VOICEVOX\VOICEVOX.exe"
    )
    if exist "C:\Program Files\VOICEVOX\VOICEVOX.exe" (
        set "VOICEVOX_EXE=C:\Program Files\VOICEVOX\VOICEVOX.exe"
    )
    if exist "C:\Program Files (x86)\VOICEVOX\VOICEVOX.exe" (
        set "VOICEVOX_EXE=C:\Program Files (x86)\VOICEVOX\VOICEVOX.exe"
    )

    if defined VOICEVOX_EXE (
        start "" "%VOICEVOX_EXE%"
        echo [OK] VOICEVOX launched. Waiting 6s for startup...
        timeout /t 6 /nobreak >nul
    ) else (
        echo [!!] VOICEVOX.exe not found.
        echo      手動で VOICEVOX を起動してから続けてください。
        echo      音声なしのテキストモードで起動します。
        echo.
    )
)

REM ── 2. Ollama チェック ───────────────────────────────────────
curl -s http://localhost:11434/api/tags >nul 2>&1
if %errorlevel% == 0 (
    echo [OK] Ollama is running
) else (
    echo [!!] Ollama が応答しません。
    echo      タスクトレイの Ollama アイコンを確認してください。
    echo.
    pause
    exit /b 1
)

REM ── 3. EchoMate 起動 ─────────────────────────────────────────
echo.
echo [..] Starting EchoMate...
echo      Ctrl+C で停止
echo.

cd /d "%~dp0"
python main.py

echo.
pause
