# apply-wirepod-config.ps1 — merge our AI config into Wire-Pod's apiConfig.json
# AFTER you've completed the first-run web UI setup. Preserves the SSL/cert
# fields Wire-Pod wrote during setup; replaces our personality / endpoint /
# intent fields.

$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$SharedDir   = Resolve-Path (Join-Path $ScriptDir "..\shared")
$WirePodDir  = Join-Path $env:USERPROFILE "vector-pod\wire-pod"
$ConfigSrc   = Join-Path $SharedDir   "config\wirepod-apiConfig.json"
$ConfigDst   = Join-Path $WirePodDir  "chipper\apiConfig.json"

if (-not (Test-Path $ConfigDst)) {
    Write-Host "[!] Wire-Pod config not found at $ConfigDst." -ForegroundColor Yellow
    Write-Host "    Has Wire-Pod's first-run setup been completed at http://localhost:8080 ?"
    exit 1
}

$python = Join-Path $env:USERPROFILE "vector-pod\vector-ai\venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

& $python -c @"
import json, sys
src, dst = r'$ConfigSrc', r'$ConfigDst'
with open(src, encoding='utf-8')  as f: our  = json.load(f)
with open(dst, encoding='utf-8')  as f: live = json.load(f)
for k in ('knowledge', 'STT', 'weather'):
    live[k] = our[k]
with open(dst, 'w', encoding='utf-8') as f: json.dump(live, f, indent=2, ensure_ascii=False)
print('Config merged.')
"@
if ($LASTEXITCODE -ne 0) { Write-Host "Merge failed." -ForegroundColor Red; exit 1 }

# Restart chipper so it picks up the new config.
Stop-ScheduledTask -TaskName "VectorPod-Chipper" -ErrorAction SilentlyContinue
Get-Process -Name chipper -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Start-ScheduledTask -TaskName "VectorPod-Chipper"
Write-Host "[+] AI config applied. Wire-Pod restarted." -ForegroundColor Green
