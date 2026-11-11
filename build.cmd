@echo off
setlocal EnableExtensions
py -3.12 -c "import struct; raise SystemExit(struct.calcsize('P') != 8)" >nul 2>&1
if not errorlevel 1 goto py312
py -3.11 -c "import struct; raise SystemExit(struct.calcsize('P') != 8)" >nul 2>&1
if not errorlevel 1 goto py311
python "%~dp0scripts\build_windows.py"
exit /b %errorlevel%
:py312
py -3.12 "%~dp0scripts\build_windows.py"
exit /b %errorlevel%
:py311
py -3.11 "%~dp0scripts\build_windows.py"
exit /b %errorlevel%
