@echo off
REM ============================================================
REM  Thunderstore Archive - Container update (double-click)
REM  Rebuilds the image and restarts it with the new code.
REM ============================================================
cd /d "%~dp0"

echo.
echo === Updating the Thunderstore Archive container ===
echo.

docker compose up -d --build
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE%==0 (
    echo Done. Interface: http://localhost:8081
) else (
    echo FAILED ^(code %EXITCODE%^) - see the messages above.
)

echo.
pause
