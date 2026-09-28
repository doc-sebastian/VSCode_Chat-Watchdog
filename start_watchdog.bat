@echo off
rem Startet den VS Code Chat-Watchdog mit GUI (ohne Konsolenfenster)
set "PYDIR=C:\### TEMP\WPy64-31020_full\python-3.10.2.amd64"

if "%~1"=="" (
    rem Ohne Argumente: GUI-Start mit pythonw.exe -> kein Konsolenfenster
    start "" "%PYDIR%\pythonw.exe" "%~dp0vscode_watchdog.py" --no-console-log
    exit /b 0
)

rem Mit Argumenten (--cli, --status, --once, ...): normale Konsole behalten
"%PYDIR%\python.exe" "%~dp0vscode_watchdog.py" %*
