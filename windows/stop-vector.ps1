# stop-vector.ps1 — shut Vector's stack down and free VRAM.
# Stopping the supervisor triggers its shutdown handler: it stops chipper
# and vector-ai and tells Ollama to unload the model.

$ErrorActionPreference = "Continue"
function Info ($m) { Write-Host "[+] $m" -ForegroundColor Green }

Write-Host "Stopping Vector..." -ForegroundColor Cyan

Stop-ScheduledTask -TaskName "VectorPod-Supervisor" -ErrorAction SilentlyContinue

# The supervisor stops its children on SIGTERM, but give it a moment and then
# sweep up anything that lingered (belt and braces).
Start-Sleep -Seconds 6
Get-Process chipper* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process | Where-Object { $_.ProcessName -like 'python*' } | ForEach-Object {
    $cli = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
    if ($cli -match 'uvicorn service:app|supervisor\.py|mdns-responder') {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
}
# Ensure the model is unloaded so VRAM is freed.
try { & ollama stop gemma4:e4b 2>$null | Out-Null } catch {}

Info "Stopped. VRAM freed."
Write-Host "Ollama itself is left running (idle, no model loaded). Start again with start-vector.ps1."
