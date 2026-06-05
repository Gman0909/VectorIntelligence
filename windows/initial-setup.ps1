# initial-setup.ps1 — Drive Wire-Pod's first-run wizard via REST.
#
# Equivalent to visiting the Wire-Pod web UI in a browser, choosing English
# STT, waiting for the VOSK model to download, and clicking "use IP mode on
# port 443". Then applies our AI config.
#
# Run ONCE after install.ps1 + start-vector.ps1.
# Idempotent — running it again is harmless.

$ErrorActionPreference = "Continue"

function Info ($msg) { Write-Host "[+] $msg" -ForegroundColor Green }
function Warn ($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Fail ($msg) { Write-Host "[X] $msg" -ForegroundColor Red; exit 1 }

# The web UI port the installer chose (pod.conf next to supervisor.py). The
# supervisor binds Wire-Pod's web server to this same value.
$WebPort = 8080
$PodConf = Join-Path $env:USERPROFILE "vector-pod\pod.conf"
if (Test-Path $PodConf) {
    $m = Get-Content $PodConf | Where-Object { $_ -match '^\s*WEB_PORT\s*=\s*(\d+)\s*$' } | Select-Object -First 1
    if ($m -match 'WEB_PORT\s*=\s*(\d+)') { $WebPort = [int]$Matches[1] }
}
$Base = "http://localhost:$WebPort"

# Sanity-check Wire-Pod is up.
try {
    Invoke-WebRequest $Base -TimeoutSec 5 -UseBasicParsing | Out-Null
} catch {
    Fail "Wire-Pod web UI not responding on port $WebPort. Run start-vector.ps1 first."
}

# Step 1: Tell Wire-Pod which STT language we want.
Info "Setting STT language to en-US..."
$resp = $null
try {
    $resp = Invoke-WebRequest "$Base/api/set_stt_info" `
        -Method POST -Body '{"language":"en-US"}' `
        -ContentType "application/json" -TimeoutSec 30 -UseBasicParsing
} catch {
    Fail "set_stt_info failed: $($_.Exception.Message). Is STT_SERVICE set to 'whisper.cpp' (or 'vosk') and has chipper restarted?"
}

# Step 2: If the VOSK model isn't already downloaded, wait for it.
if ($resp.Content -match "downloading") {
    Info "Downloading VOSK English model (~50 MB)..."
    while ($true) {
        Start-Sleep -Seconds 5
        $status = (Invoke-WebRequest "$Base/api/get_download_status" -UseBasicParsing).Content
        Write-Host "    status: $status"
        if ($status -match "success")          { Info "VOSK model installed."; break }
        if ($status -match "error")            { Fail "VOSK download failed: $status" }
        if ($status -match "not downloading")  { break }
    }
}

# Step 3: Switch Wire-Pod to "escape pod" mode (epconfig=true). The Pi
# deployment runs in this mode, and the wpsetup.keriganc.com pairing flow
# expects it — Vector gets handed `escapepod.local:443` as the server endpoint
# and resolves it via mDNS to our machine. IP mode (use_ip) was the alternative
# but caused pairing activation to fail because of how wpsetup pushes config.
Info "Switching Wire-Pod to escape pod mode and binding port 443..."
try {
    $resp = Invoke-WebRequest "$Base/api-chipper/use_ep" -TimeoutSec 30 -UseBasicParsing
    Info "Wire-Pod is now in escape pod mode, serving on :443."
} catch {
    Fail "use_ep failed: $($_.Exception.Message)"
}

Start-Sleep -Seconds 3
$bound443 = Get-NetTCPConnection -LocalPort 443 -State Listen -ErrorAction SilentlyContinue
if (-not $bound443) { Warn "Port 443 still not listening. Check the chipper task status." }

# Step 4: Apply our personality + intent endpoint config on top of what Wire-Pod
# just wrote (which has the cert and server.ip fields it generated).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Info "Applying our AI config (personality, vector-ai endpoint)..."
& powershell -ExecutionPolicy Bypass -File (Join-Path $ScriptDir "apply-wirepod-config.ps1")

Write-Host ""
Write-Host "Initial setup complete." -ForegroundColor Green
Write-Host "  Next step: open $Base, go to the Robots tab, and pair Vector."
