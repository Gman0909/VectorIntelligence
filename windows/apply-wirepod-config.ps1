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

# Restart the stack so chipper re-reads apiConfig.json. The supervisor owns
# chipper (and vector-ai / Ollama), so bouncing its task is how the new config
# takes effect — chipper runs elevated as the supervisor's child, so stopping
# it directly isn't reliable from a non-admin shell. Controlling the supervisor
# task needs no elevation (same as start-vector.ps1 / stop-vector.ps1).
$sup = Get-ScheduledTask -TaskName "VectorPod-Supervisor" -ErrorAction SilentlyContinue
if ($sup) {
    Stop-ScheduledTask  -TaskName "VectorPod-Supervisor" -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName "VectorPod-Supervisor"
    Write-Host "[+] AI config applied. Supervisor restarted — give it ~15s to bring chipper back up." -ForegroundColor Green
} else {
    Write-Host "[+] AI config merged. Start the stack with start-vector.ps1 to load it." -ForegroundColor Green
}
