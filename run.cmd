@echo off
rem ratiomaster launcher for Windows.
rem   run.cmd [options]          -> python ratiomaster.py [options]
rem   drag a .torrent onto it    -> python ratiomaster.py "that.torrent"
rem   double-click (no args)     -> mode taken from config.ini [general] mode
rem Set RM_NOPAUSE=1 to skip the final "pause" (e.g. when called from scripts).
setlocal

set "RM_SCRIPT=%~dp0ratiomaster.py"
set "RM_PY="

rem Prefer the "py" launcher, fall back to "python" (ignores the Microsoft Store stub,
rem which fails on --version).
py -3 --version >nul 2>&1 && set "RM_PY=py -3"
if not defined RM_PY (python --version >nul 2>&1 && set "RM_PY=python")

if not defined RM_PY (
    echo.
    echo [!] Python 3.10+ was not found.
    echo     Install it from https://www.python.org/downloads/
    echo     and tick "Add python.exe to PATH" during setup.
    echo     Or use the standalone ratiomaster.exe instead - see README.md.
    echo.
    set "RM_RC=1"
    goto :end
)

rem A .torrent dropped onto this file arrives as %1 with its full quoted path, so it
rem simply runs in CLI mode; any extra options after it (e.g. --dry-run) are kept.
if "%~1"=="" (
    %RM_PY% "%RM_SCRIPT%"
) else (
    %RM_PY% "%RM_SCRIPT%" %*
)
set "RM_RC=%ERRORLEVEL%"

:end
if not defined RM_NOPAUSE pause
exit /b %RM_RC%
