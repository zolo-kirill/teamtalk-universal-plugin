@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0windows\launcher.py" %*
) else (
    python "%~dp0windows\launcher.py" %*
)
set RC=%ERRORLEVEL%
if "%~1"=="" if not "%RC%"=="0" pause
exit /b %RC%
