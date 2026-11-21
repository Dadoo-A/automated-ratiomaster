@echo off
rem Builds a single-file Windows executable: dist\ratiomaster.exe
rem The .exe runs on PCs without Python. Requires PyInstaller (build machine only).
setlocal
cd /d "%~dp0"

set "RM_PY="
py -3 --version >nul 2>&1 && set "RM_PY=py -3"
if not defined RM_PY (python --version >nul 2>&1 && set "RM_PY=python")
if not defined RM_PY (
    echo [!] Python 3.10+ is needed to BUILD the exe: https://www.python.org/downloads/
    set "RM_RC=1"
    goto :end
)

%RM_PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [!] PyInstaller is not installed. Install it with:
    echo         %RM_PY% -m pip install pyinstaller
    echo     then run build_exe.cmd again.
    set "RM_RC=1"
    goto :end
)

%RM_PY% -m PyInstaller --onefile --name ratiomaster ratiomaster.py
set "RM_RC=%ERRORLEVEL%"
if not "%RM_RC%"=="0" (
    echo [!] Build failed ^(exit code %RM_RC%^).
    goto :end
)

echo.
echo Done: dist\ratiomaster.exe
echo It runs on PCs without Python. Put config.ini next to it to change the defaults
echo ^(otherwise built-in defaults are used^); the watch\ folder is created next to it.

:end
if not defined RM_NOPAUSE pause
exit /b %RM_RC%
