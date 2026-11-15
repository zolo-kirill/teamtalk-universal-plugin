@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
if "%~1"=="" goto gui
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install.ps1" %*
goto done
:gui
powershell -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0windows\install-gui.ps1"
:done
set RC=%ERRORLEVEL%
if not "%RC%"=="0" pause
exit /b %RC%
