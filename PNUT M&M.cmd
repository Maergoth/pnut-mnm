@echo off
rem PNUT M&M launcher: runs the desktop app with the venv's console-less interpreter.
rem Extra arguments are passed through (e.g. -v, --selftest-seconds 10, --config PATH).
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" -m mnmparse.app %*
