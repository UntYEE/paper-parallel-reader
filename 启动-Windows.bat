@echo off
setlocal
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "PPR_EXIT=%ERRORLEVEL%"
if not "%PPR_EXIT%"=="0" pause
exit /b %PPR_EXIT%
