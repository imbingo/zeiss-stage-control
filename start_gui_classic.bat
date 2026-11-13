@echo off
setlocal
cd /d "%~dp0"
rem Classic Tk interface, kept as a fallback.
if defined ZEN_PYTHON (
    "%ZEN_PYTHON%" -B zen_gui.py
) else (
    if exist "runtime\python.exe" (
        "runtime\python.exe" -I -B -c "import sys,runpy; sys.path.insert(0,'.'); runpy.run_module('zen_gui',run_name='__main__')"
        exit /b
    )
    where py >nul 2>&1
    if errorlevel 1 (
        python -B zen_gui.py
    ) else (
        py -3 -B zen_gui.py
    )
)
exit /b %errorlevel%
