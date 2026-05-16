@echo off
title Vector - Start
REM Self-contained — starts the VectorPod-Supervisor scheduled task.
REM The supervisor brings up Ollama, Wire-Pod and vector-ai, advertises
REM mDNS, and auto-recovers from drops/sleep/IP changes. No admin needed.

echo Starting Vector...
schtasks /run /tn "VectorPod-Supervisor" >nul 2>&1
if errorlevel 1 (
    echo [!] VectorPod-Supervisor task not found. Run install.cmd first.
    echo.
    pause
    exit /b 1
)
echo [+] Supervisor started.
echo     Bringing up Ollama, Wire-Pod and vector-ai - give it ~20 seconds.
echo     Then say "Hey Vector" to chat.
echo.
echo     Stop with stop-vector.cmd when done (frees VRAM).
timeout /t 6 >nul
