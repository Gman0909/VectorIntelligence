@echo off
REM Double-click wrapper for install.ps1. Self-elevates to admin via the PS1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
