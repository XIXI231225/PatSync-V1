@echo off
chcp 65001 >nul
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" -InstallDependencies
if errorlevel 1 goto failed
exit /b 0
:failed
pause
exit /b 1
