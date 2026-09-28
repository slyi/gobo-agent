@echo off
rem Thin wrapper around setup.ps1, for when double-clicking or cmd.exe is allowed.
rem Where .cmd files are blocked by group policy, run setup.ps1 directly:
rem     powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
exit /b %errorlevel%
