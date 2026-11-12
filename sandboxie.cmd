@echo off
setlocal EnableExtensions DisableDelayedExpansion
if not exist "%~dp0.venv-build\Scripts\python.exe" goto trydevenv
"%~dp0.venv-build\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto buildenv
:trydevenv
if not exist "%~dp0.venv\Scripts\python.exe" goto trypy
"%~dp0.venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto devenv
:trypy
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto pylauncher
python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto python
echo Python 3.11 or newer was not found. Install Python or run build.cmd first.
exit /b 1
:buildenv
"%~dp0.venv-build\Scripts\python.exe" -B "%~dp0scripts\launch_zwift_sandboxie.py" %*
exit /b %errorlevel%
:devenv
"%~dp0.venv\Scripts\python.exe" -B "%~dp0scripts\launch_zwift_sandboxie.py" %*
exit /b %errorlevel%
:pylauncher
py -3 "%~dp0scripts\launch_zwift_sandboxie.py" %*
exit /b %errorlevel%
:python
python "%~dp0scripts\launch_zwift_sandboxie.py" %*
exit /b %errorlevel%
