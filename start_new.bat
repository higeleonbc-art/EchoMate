@echo off
chcp 65001 >nul
title EchoMate

echo ============================================================
echo   EchoMate - Game AI Companion
echo ============================================================
echo.

cd /d "%~dp0"

REM GUI を起動（VOICEVOX / Ollama の管理はGUIが行います）
python gui.py

echo.
pause
