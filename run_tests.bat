@echo off
chcp 65001 >nul
title ADC Coach - Run Tests
cd /d "%~dp0"

echo ============================================================
echo   Running ADC Coach test suite
echo ============================================================
echo.

set FAIL=0
for %%f in (tests\test_*.py) do (
    echo === %%f ===
    python "%%f"
    if errorlevel 1 set FAIL=1
    echo.
)

if "%FAIL%"=="0" (
    echo ============================================================
    echo   All tests passed.
    echo ============================================================
) else (
    echo ============================================================
    echo   Some tests FAILED.
    echo ============================================================
)
pause
