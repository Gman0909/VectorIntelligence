@echo off
REM Double-click wrapper for initial-setup.ps1 (Wire-Pod first-run wizard).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0initial-setup.ps1"
pause
