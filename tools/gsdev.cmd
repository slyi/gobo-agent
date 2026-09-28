@echo off
rem Thin wrapper around gsdev.ps1, for when cmd.exe is allowed.
rem Where .cmd files are blocked by group policy, run tools\gsdev.ps1 directly:
rem     powershell -NoProfile -ExecutionPolicy Bypass -File tools\gsdev.ps1 doctor
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gsdev.ps1" %*
exit /b %errorlevel%
