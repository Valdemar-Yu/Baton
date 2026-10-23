@echo off
setlocal DisableDelayedExpansion
python "%~dp0..\scripts\baton.py" %*
exit /b %errorlevel%
