@echo off
rem ---------------------------------------------------------------------------
rem  Escape Room Zagreb availability explorer
rem
rem    run.cmd              serve on http://127.0.0.1:8765/  (uses cached data)
rem    run.cmd 9000         serve on another port
rem    run.cmd -rescan      re-scrape everything first, then serve
rem
rem  Works regardless of the PowerShell execution policy (which is Restricted
rem  on this machine and therefore blocks run.ps1).
rem ---------------------------------------------------------------------------
setlocal
set "HERE=%~dp0"
set "PYTHONIOENCODING=utf-8"
cd /d "%HERE%"

rem --- find a Python: the bundled runtime first, then whatever is on PATH ------
set "PY=C:\Users\dewat\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"

set "PORT=8765"
set "RESCAN="
for %%A in (%*) do (
    if /I "%%~A"=="-rescan"  set "RESCAN=1"
    if /I "%%~A"=="--rescan" set "RESCAN=1"
    echo %%~A| findstr /r /x "[0-9][0-9]*" >nul
    if not errorlevel 1 set "PORT=%%~A"
)

echo Using Python: %PY%
"%PY%" --version
if errorlevel 1 goto :err

if not exist "data\rooms.json" (
    echo.
    echo Building the room catalogue from escaperoomzagreb.com ...
    "%PY%" catalog.py
    if errorlevel 1 goto :err
)

if defined RESCAN goto :scan
if not exist "data\availability.json" goto :scan
goto :serve

:scan
echo.
echo Scanning availability from every provider ^(this takes 1-5 minutes^) ...
"%PY%" aggregate.py 30
if errorlevel 1 goto :err

:serve
echo.
echo   ---------------------------------------------------------------
echo    Serving on  http://127.0.0.1:%PORT%/
echo    Press Ctrl+C to stop, then close this window.
echo   ---------------------------------------------------------------
echo.
"%PY%" server.py --port %PORT%
goto :eof

:err
echo.
echo *** Something failed - read the message above. ***
pause
