@echo off
chcp 65001 >nul
title LoL ADC Coach
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ============================================================
echo   LoL ADC Coach
echo ============================================================
echo.

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

:MENU
echo ------------------------------------------------------------
echo   Select mode:
echo ------------------------------------------------------------
echo   [1] Live overlay        (in-game translucent overlay)
echo   [2] Review latest match (pick from recent 10 matches)
echo   [3] Review last 3 matches (any queue)
echo   [4] Review last 3 ranked solo
echo   [5] Demo: overlay       (no LoL needed)
echo   [6] Demo: review HTML   (no LoL needed)
echo   [Q] Quit
echo ------------------------------------------------------------
set /p MODE=Select:

if /i "!MODE!"=="1" goto LIVE
if /i "!MODE!"=="2" goto PICK_MATCH
if /i "!MODE!"=="3" goto REVIEW3_ANY
if /i "!MODE!"=="4" goto REVIEW3_RANKED
if /i "!MODE!"=="5" goto DEMO_OVERLAY
if /i "!MODE!"=="6" goto DEMO_REVIEW
if /i "!MODE!"=="Q" goto END
echo Invalid choice.
echo.
goto MENU

:LIVE
echo.
set /p RANK=Target rank (GOLD/PLATINUM/MASTER) [GOLD]:
if "!RANK!"=="" set RANK=GOLD
echo Starting live overlay. Press ESC to quit.
python coach_main.py --live --rank !RANK!
goto END

:PICK_MATCH
echo.
set /p RIOT_ID=Riot ID (Name#TAG):
if "!RIOT_ID!"=="" goto MENU
set /p RANK=Target rank [auto = current+1]:
if "!RANK!"=="" set RANK=auto
python coach_pick.py --riot-id "!RIOT_ID!" --rank !RANK!
goto END

:REVIEW3_ANY
echo.
set /p RIOT_ID=Riot ID (Name#TAG):
if "!RIOT_ID!"=="" goto MENU
set /p RANK=Target rank [auto = current+1]:
if "!RANK!"=="" set RANK=auto
python coach_main.py --riot-id "!RIOT_ID!" --count 3 --rank !RANK! --view html
goto END

:REVIEW3_RANKED
echo.
set /p RIOT_ID=Riot ID (Name#TAG):
if "!RIOT_ID!"=="" goto MENU
set /p RANK=Target rank [auto = current+1]:
if "!RANK!"=="" set RANK=auto
python coach_main.py --riot-id "!RIOT_ID!" --count 3 --queue 420 --rank !RANK! --view html
goto END

:DEMO_OVERLAY
python coach_overlay.py
goto END

:DEMO_REVIEW
python coach_review_view.py
goto END

:END
echo.
pause
endlocal
