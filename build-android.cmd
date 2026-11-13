@echo off
setlocal
python "%~dp0scripts\build_android.py" %*
exit /b %ERRORLEVEL%
