# install.ps1 — One-time Windows setup for the Vector AI stack.
#
# Installs and configures everything in-place on this machine:
#   - Go, Python, Git, MSYS2 (mingw, for cgo), Ollama via winget
#   - Wire-Pod (cloned from upstream, our patches applied, built locally)
#   - vector-ai Python service (FastAPI proxy)
#   - gemma3:12b model
#   - Windows Firewall rules and Scheduled Tasks for daily start/stop
#
# Run from an elevated (admin) PowerShell:  .\install.ps1
# After install completes:  daily use is start-vector.ps1 / stop-vector.ps1

#Requires -Version 5.1
$ErrorActionPreference = "Stop"

# ── Self-elevate if not running as admin ──────────────────────────────────────
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Re-launching as administrator..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

# ── Paths ─────────────────────────────────────────────────────────────────────
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$SharedDir   = Resolve-Path (Join-Path $ScriptDir "..\shared")
$InstallRoot = Join-Path $env:USERPROFILE "vector-pod"
$WirePodDir  = Join-Path $InstallRoot "wire-pod"
$VectorAIDir = Join-Path $InstallRoot "vector-ai"
$VoskDir     = Join-Path $WirePodDir  "chipper\vosk"

# ── Pinned upstream commits ───────────────────────────────────────────────────
# Our patch scripts are written against these exact revisions. Bumping them
# is a deliberate, re-test-everything decision — never float to HEAD, or a
# future upstream change will silently break the patches for new installers.
$WirePodCommit = "11e7b22095166ed35765e88a8a10ed3a6ce49d5c"
$WhisperCommit = "60cd96acff3a72895cb9ae9cbabe9de21b1e9125"
$SdkCommit     = "62168f3595d67ae0bf24103a9fe1fc5f2eb9b85c"

function Step    ($msg) { Write-Host "`n── $msg ──" -ForegroundColor Cyan }
function Info    ($msg) { Write-Host "[+] $msg"     -ForegroundColor Green }
function Warn    ($msg) { Write-Host "[!] $msg"     -ForegroundColor Yellow }
function Fail    ($msg) { Write-Host "[X] $msg"     -ForegroundColor Red; exit 1 }
function CmdExists ($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

# Detects real installs vs. Microsoft Store "open the Store" stubs (which are
# 0-byte placeholders that PowerShell finds via PATH). A real install resolves
# to a binary at least a few KB in size. Avoids running --version which has
# inconsistent syntax across tools (go uses `go version`, etc.).
function CmdWorks ($name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { return $false }
    if (-not (Test-Path $cmd.Source)) { return $false }
    $size = (Get-Item $cmd.Source).Length
    return $size -gt 1024
}

# ── 1. Prerequisites via winget ───────────────────────────────────────────────
Step "Prerequisites"

if (-not (CmdExists "winget")) {
    Fail "winget is not available. Install 'App Installer' from the Microsoft Store first."
}

function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
}

function Install-IfMissing ($cmd, $id, $label) {
    Refresh-Path
    if (CmdWorks $cmd) { Info "$label already installed."; return }
    Info "Installing $label via winget..."
    winget install --id $id --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
    Refresh-Path
    # winget can return non-zero for "already installed, no upgrade available"
    # and other benign reasons. Re-check by actually running the command.
    if (-not (CmdWorks $cmd)) { Fail "Failed to install $label — command still not working after winget." }
    Info "$label installed."
}
Install-IfMissing "go"     "GoLang.Go"             "Go"
Install-IfMissing "python" "Python.Python.3.11"    "Python 3.11"
Install-IfMissing "git"    "Git.Git"               "Git"
Install-IfMissing "ollama" "Ollama.Ollama"         "Ollama"

# MSYS2 provides the mingw-w64 toolchain we need for cgo (Wire-Pod's VOSK +
# Opus bindings both go through cgo).
if (-not (Test-Path "C:\msys64\mingw64\bin\gcc.exe")) {
    Info "Installing MSYS2 (provides mingw64 toolchain for cgo)..."
    winget install --id MSYS2.MSYS2 --silent --accept-package-agreements --accept-source-agreements
    if (-not (Test-Path "C:\msys64\mingw64\bin\gcc.exe")) {
        Info "Updating MSYS2 base..."
        & "C:\msys64\usr\bin\bash.exe" -lc "pacman -Syu --noconfirm" | Out-Host
    }
}
# These are all required for the chipper build: gcc (compiler), pkgconf
# (Opus binding probes it), opus/opusfile (Vector's audio codec).
Info "Ensuring mingw-w64 toolchain packages are installed..."
& "C:\msys64\usr\bin\bash.exe" -lc "pacman -S --noconfirm --needed mingw-w64-x86_64-gcc mingw-w64-x86_64-pkgconf mingw-w64-x86_64-opus mingw-w64-x86_64-opusfile" | Out-Host
$MingwBin = "C:\msys64\mingw64\bin"
if (-not (Test-Path "$MingwBin\gcc.exe"))     { Fail "mingw64 gcc not found at $MingwBin\gcc.exe" }
if (-not (Test-Path "$MingwBin\pkgconf.exe")) { Fail "pkgconf not found at $MingwBin\pkgconf.exe" }
Info "mingw-w64 toolchain ready at $MingwBin"

# Refresh PATH so newly installed tools are visible to this session.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path","User") + ";" + $MingwBin

# ── 2. OLLAMA_HOST ────────────────────────────────────────────────────────────
Step "Ollama configuration"
if ([System.Environment]::GetEnvironmentVariable("OLLAMA_HOST","Machine") -ne "0.0.0.0") {
    [System.Environment]::SetEnvironmentVariable("OLLAMA_HOST","0.0.0.0","Machine")
    Info "Set OLLAMA_HOST=0.0.0.0 (System scope). Restart Ollama for it to take effect."
} else {
    Info "OLLAMA_HOST already set to 0.0.0.0."
}

# Ollama's installer drops a Startup-folder shortcut so it auto-launches at
# logon. start-vector.ps1 already starts it on demand — remove the auto-launch
# so nothing related to Vector commits resources unless we ask for it.
$ollamaStartup = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Ollama.lnk"
if (Test-Path $ollamaStartup) {
    Remove-Item $ollamaStartup -Force
    Info "Removed Ollama auto-launch from Windows startup."
} else {
    Info "Ollama auto-launch already absent."
}

# ── 3. Wire-Pod: clone (must come before VOSK so the wire-pod dir doesn't ────
#       already exist when git clone tries to create it)
Step "Wire-Pod clone"
New-Item -ItemType Directory -Force $InstallRoot | Out-Null
if (-not (Test-Path "$WirePodDir\.git")) {
    Info "Cloning Wire-Pod..."
    git clone "https://github.com/kercre123/wire-pod" $WirePodDir
    if ($LASTEXITCODE -ne 0) { Fail "git clone failed." }
}
# Pin to the exact commit our patches are written against (see $WirePodCommit).
Info "Checking out pinned Wire-Pod commit..."
Push-Location $WirePodDir
git fetch --quiet origin $WirePodCommit 2>$null
git checkout --quiet $WirePodCommit
$coExit = $LASTEXITCODE
Pop-Location
if ($coExit -ne 0) { Fail "Could not check out pinned Wire-Pod commit $WirePodCommit." }

# ── 4. VOSK Windows binary ────────────────────────────────────────────────────
Step "VOSK runtime"
$VoskVersion = "0.3.45"
$VoskZip     = Join-Path $env:TEMP "vosk-win64.zip"
if (-not (Test-Path "$VoskDir\libvosk.dll")) {
    New-Item -ItemType Directory -Force $VoskDir | Out-Null
    Info "Downloading vosk-win64-$VoskVersion..."
    Invoke-WebRequest "https://github.com/alphacep/vosk-api/releases/download/v$VoskVersion/vosk-win64-$VoskVersion.zip" -OutFile $VoskZip
    Expand-Archive -Path $VoskZip -DestinationPath $env:TEMP -Force
    Copy-Item "$env:TEMP\vosk-win64-$VoskVersion\*" $VoskDir -Force
    Remove-Item $VoskZip -Force
    Info "VOSK installed to $VoskDir"
} else {
    Info "VOSK already present."
}

# ── 5. Wire-Pod patches ───────────────────────────────────────────────────────
Step "Wire-Pod patches"

Info "Patching listening timeout (460ms -> 1.5s of silence)..."
$VadFile = Join-Path $WirePodDir "chipper\pkg\wirepod\speechrequest\speechrequest.go"
$vadSrc = Get-Content $VadFile -Raw
$newVad = $vadSrc -replace 'inactiveNumMax := (?:23|150|100|75)[^\r\n]*', 'inactiveNumMax := 75 // 1.5s of silence'
if ($newVad -ne $vadSrc) {
    $newVad | Set-Content $VadFile -NoNewline
    Info "VAD patch applied (2s)."
} else {
    Info "VAD line not found — Wire-Pod source may have changed."
}

Info "Expanding animation vocabulary..."
python "$SharedDir\patches\expand-animations.py" (Join-Path $WirePodDir "chipper\pkg\wirepod\ttr\kgsim_cmds.go")

Info "Adding wake-word interrupt grace period..."
python "$SharedDir\patches\wake-word-grace-period.py" (Join-Path $WirePodDir "chipper\pkg\wirepod\ttr\kgsim_interrupt.go")

Info "Making the back button interrupt Vector's speech..."
python "$SharedDir\patches\add-button-interrupt.py" (Join-Path $WirePodDir "chipper\pkg\wirepod\ttr\kgsim_interrupt.go")

Info "Muting wake-word interrupts during getImage (stops Vector's own shutter sound self-interrupting)..."
python "$SharedDir\patches\wake-word-mute-during-getimage.py" $WirePodDir

Info "Adding on-demand face detection (per-interaction only, never a 24/7 firehose)..."
python "$SharedDir\patches\add-ondemand-face.py" (Join-Path $WirePodDir "chipper\pkg\wirepod\ttr\kgsim_interrupt.go")

Info "Removing photo viewfinder + 3-2-1 countdown (shutter animation stays — it's our audio cue)..."
python "$SharedDir\patches\remove-photo-countdown.py" (Join-Path $WirePodDir "chipper\pkg\wirepod\ttr\kgsim_cmds.go")

Info "Routing 'dance' and 'lookAtUser' aliases to Vector's built-in behaviours..."
python "$SharedDir\patches\use-builtin-behaviors.py" (Join-Path $WirePodDir "chipper\pkg\wirepod\ttr\kgsim_cmds.go")

Info "On vision queries, dispatch intent_imperative_lookatme BEFORE the LLM so Vector rapid-turns using his fresh mic-direction cache..."
python "$SharedDir\patches\prelim-lookatme-then-llm.py" (Join-Path $WirePodDir "chipper\pkg\wirepod\preqs\intent_graph.go")

Info "Slowing Vector's TTS slightly for clarity..."
python "$SharedDir\patches\slow-tts.py" (Join-Path $WirePodDir "chipper\pkg\wirepod\ttr\kgsim_cmds.go")

Info "Adding LLM-driven eye colour command (mood expression)..."
python "$SharedDir\patches\add-eye-color-cmd.py" (Join-Path $WirePodDir "chipper\pkg\wirepod\ttr\kgsim_cmds.go")

Info "Adding background sensor reactions (pickup, putdown, pet)..."
python "$SharedDir\patches\add-sensor-reactions.py" $WirePodDir

Info "Fixing the gRPC connection leak (defer robot.Close() in kgsim.go)..."
python "$SharedDir\patches\fix-connection-leak.py" (Join-Path $WirePodDir "chipper\pkg\wirepod\ttr\kgsim.go")

Info "Fixing name parsing so face enrollment captures just the name..."
python "$SharedDir\patches\fix-name-extraction.py" (Join-Path $WirePodDir "chipper\pkg\wirepod\ttr\intentparam.go")

Info "Adding the concurrent face probe (knows the speaker before the LLM replies)..."
python "$SharedDir\patches\add-face-probe.py" $WirePodDir

# ── Patched vector-go-sdk ─────────────────────────────────────────────────────
# The upstream SDK opens a gRPC connection per vector.New() but never closes
# it, so every voice query leaks one until the robot's SDK wedges. Pull the
# pinned SDK commit into chipper\third_party, patch in a Close() method, and
# point chipper's go.mod at the local copy via a replace directive.
Step "Patched vector-go-sdk"
$SdkDir = Join-Path $WirePodDir "chipper\third_party\vector-go-sdk"
if (-not (Test-Path "$SdkDir\go.mod")) {
    Info "Cloning vector-go-sdk into chipper\third_party..."
    New-Item -ItemType Directory -Force (Split-Path $SdkDir) | Out-Null
    git clone "https://github.com/fforchino/vector-go-sdk" $SdkDir | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "vector-go-sdk clone failed." }
    Push-Location $SdkDir
    git fetch --quiet origin $SdkCommit 2>$null
    git checkout --quiet $SdkCommit
    $sdkExit = $LASTEXITCODE
    Pop-Location
    if ($sdkExit -ne 0) { Fail "Could not check out pinned vector-go-sdk commit $SdkCommit." }
    # Drop .git — this is now a vendored local module, not a clone to update.
    Remove-Item -Recurse -Force (Join-Path $SdkDir ".git") -ErrorAction SilentlyContinue
}
# add-sdk-close.py is idempotent — safe to run on every install.
python "$SharedDir\patches\add-sdk-close.py" (Join-Path $SdkDir "pkg\vector\vector.go")
# Redirect chipper's dependency to the local patched copy.
$ChipperGoMod = Join-Path $WirePodDir "chipper\go.mod"
if ((Get-Content $ChipperGoMod -Raw) -notmatch 'replace github\.com/fforchino/vector-go-sdk') {
    Add-Content $ChipperGoMod "`nreplace github.com/fforchino/vector-go-sdk => ./third_party/vector-go-sdk"
    Info "go.mod now points at the patched vector-go-sdk."
} else {
    Info "go.mod replace directive already present."
}

Info "Installing minimal intent file (come here + go home)..."
Copy-Item "$SharedDir\config\wirepod-intents-en-US.json" (Join-Path $WirePodDir "chipper\intent-data\en-US.json") -Force

Info "Building chipper.exe (a few minutes, downloads Go deps the first time)..."
$env:CGO_ENABLED   = "1"
$env:CC            = "$MingwBin\gcc.exe"
$env:CGO_CFLAGS    = "-I$VoskDir"
$env:CGO_LDFLAGS   = "-L$VoskDir"
$GoBin             = "C:\Program Files\Go\bin"
$env:PATH          = "$GoBin;$MingwBin;$VoskDir;" + $env:PATH
Push-Location (Join-Path $WirePodDir "chipper")
& go build -o chipper.exe .\cmd\vosk
$buildExit = $LASTEXITCODE
Pop-Location
if ($buildExit -ne 0) { Fail "chipper build failed (exit $buildExit). See output above." }
# Copy runtime DLLs into the chipper dir so it can launch from a Scheduled Task
# without inheriting MSYS2's PATH. libvosk loads VOSK, the mingw libs are
# gcc/Opus/OpenSSL runtime dependencies of chipper.exe.
$runtimeDlls = @(
    "$VoskDir\libvosk.dll",
    "$MingwBin\libgcc_s_seh-1.dll",
    "$MingwBin\libwinpthread-1.dll",
    "$MingwBin\libstdc++-6.dll",
    "$MingwBin\libopus-0.dll",
    "$MingwBin\libogg-0.dll",
    "$MingwBin\libopusfile-0.dll",
    "$MingwBin\libssl-3-x64.dll",
    "$MingwBin\libcrypto-3-x64.dll"
)
$chipperOut = Join-Path $WirePodDir "chipper"
foreach ($dll in $runtimeDlls) {
    if (Test-Path $dll) { Copy-Item $dll $chipperOut -Force }
}
Info "chipper.exe (VOSK) built (runtime DLLs bundled)."

# ── 4b. Whisper.cpp STT (better accuracy than VOSK) ──────────────────────────
# We build a second chipper binary using the whisper.cpp STT backend. A
# small launch dispatcher picks one at runtime based on the STT_SERVICE env
# var. Whisper is the default (better at names, accents, full sentences);
# falling back to VOSK is a one-line env-var change.
Step "Whisper.cpp STT"

# Install cmake + make in MSYS2 if not present (needed to build libwhisper).
if (-not (Test-Path "$MingwBin\cmake.exe")) {
    Info "Installing mingw cmake + make via MSYS2 pacman..."
    & "C:\msys64\usr\bin\bash.exe" -lc "pacman -S --needed --noconfirm mingw-w64-x86_64-cmake mingw-w64-x86_64-make" | Out-Null
}

$WhisperRepo = Join-Path $WirePodDir "whisper.cpp"
if (-not (Test-Path "$WhisperRepo\.git")) {
    Info "Cloning whisper.cpp..."
    git clone https://github.com/kercre123/whisper.cpp.git $WhisperRepo | Out-Null
}
# Pin whisper.cpp too — its cgo binding lives in the pinned Wire-Pod tree.
Info "Checking out pinned whisper.cpp commit..."
Push-Location $WhisperRepo
git fetch --quiet origin $WhisperCommit 2>$null
git checkout --quiet $WhisperCommit
$wcoExit = $LASTEXITCODE
Pop-Location
if ($wcoExit -ne 0) { Fail "Could not check out pinned whisper.cpp commit $WhisperCommit." }

$WhisperBuild = Join-Path $WhisperRepo "build_go"
if (-not (Test-Path "$WhisperBuild\bin\libwhisper.dll")) {
    Info "Building libwhisper (CPU, ~2-3 min)..."
    Push-Location $WhisperRepo
    & "$MingwBin\cmake.exe" -B build_go -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DWHISPER_BUILD_EXAMPLES=OFF -DWHISPER_BUILD_TESTS=OFF | Out-Null
    if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "whisper.cpp cmake configure failed." }
    & "$MingwBin\cmake.exe" --build build_go --config Release -j 4 | Out-Null
    if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "whisper.cpp build failed." }
    Pop-Location
}

# base.en is the default: ~2x faster than small.en on CPU (~0.5s vs ~1.5s
# per utterance) with accuracy still well above VOSK. Switch to small.en
# (setx WHISPER_MODEL small.en /M) if you want maximum accuracy and have
# the CPU headroom, or build the Vulkan GPU backend.
$WhisperModel = Join-Path $WhisperRepo "models\ggml-base.en.bin"
if (-not (Test-Path $WhisperModel)) {
    Info "Downloading Whisper base.en model (~142 MB)..."
    $url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
    Invoke-WebRequest -Uri $url -OutFile $WhisperModel -UseBasicParsing
}
Info "Whisper model: $([math]::Round((Get-Item $WhisperModel).Length / 1MB, 1)) MB"

Info "Building chipper-whisper.exe..."
$env:CGO_ENABLED   = "1"
$env:CC            = "$MingwBin\gcc.exe"
$env:CGO_CFLAGS    = "-I$WhisperRepo\include -I$WhisperRepo\ggml\include"
$env:CGO_LDFLAGS   = "-L$WhisperRepo\build_go\src -L$WhisperRepo\build_go\ggml\src -lwhisper"
Push-Location (Join-Path $WirePodDir "chipper")
& go build -o chipper-whisper.exe .\cmd\experimental\whisper.cpp
$whisperBuildExit = $LASTEXITCODE
Pop-Location
if ($whisperBuildExit -ne 0) { Fail "chipper-whisper build failed (exit $whisperBuildExit)." }

# Copy whisper runtime DLLs + libgomp (OpenMP) into chipper dir.
$whisperDlls = @(
    "$WhisperRepo\build_go\bin\libwhisper.dll",
    "$WhisperRepo\build_go\bin\ggml.dll",
    "$WhisperRepo\build_go\bin\ggml-base.dll",
    "$WhisperRepo\build_go\bin\ggml-cpu.dll",
    "$MingwBin\libgomp-1.dll"
)
foreach ($dll in $whisperDlls) {
    if (Test-Path $dll) { Copy-Item $dll $chipperOut -Force }
}

Info "chipper-whisper.exe built (the supervisor selects it over the VOSK build)."

# Default to Whisper unless the user previously chose VOSK. WHISPER_MODEL
# tells the chipper-whisper which model file to load.
if (-not [System.Environment]::GetEnvironmentVariable("STT_SERVICE","Machine")) {
    [System.Environment]::SetEnvironmentVariable("STT_SERVICE","whisper","Machine")
    Info "Set STT_SERVICE=whisper (default). To revert to VOSK: setx STT_SERVICE vosk /M"
}
if (-not [System.Environment]::GetEnvironmentVariable("WHISPER_MODEL","Machine")) {
    [System.Environment]::SetEnvironmentVariable("WHISPER_MODEL","base.en","Machine")
    Info "Set WHISPER_MODEL=base.en (system env)."
}

# DISABLE_MDNS=true silences chipper's built-in mDNS broadcaster, which spams
# `[WARN] mdns: Failed to set multicast interface` on Windows. Our Python
# VectorPod-MDNS task already advertises escapepod.local + the service record.
if ([System.Environment]::GetEnvironmentVariable("DISABLE_MDNS","Machine") -ne "true") {
    [System.Environment]::SetEnvironmentVariable("DISABLE_MDNS","true","Machine")
    Info "Set DISABLE_MDNS=true (system env) — Python responder handles mDNS."
}

# ── 5. vector-ai Python service ───────────────────────────────────────────────
Step "vector-ai"
New-Item -ItemType Directory -Force $VectorAIDir | Out-Null
Copy-Item "$SharedDir\vector-ai\service.py"       (Join-Path $VectorAIDir "service.py")       -Force
Copy-Item "$SharedDir\vector-ai\memory.py"        (Join-Path $VectorAIDir "memory.py")        -Force
Copy-Item "$SharedDir\vector-ai\requirements.txt" (Join-Path $VectorAIDir "requirements.txt") -Force
if (-not (Test-Path (Join-Path $VectorAIDir ".env"))) {
    Copy-Item "$SharedDir\vector-ai\.env" (Join-Path $VectorAIDir ".env") -Force
}
if (-not (Test-Path (Join-Path $VectorAIDir "venv"))) {
    Info "Creating Python venv..."
    python -m venv (Join-Path $VectorAIDir "venv")
}
Info "Installing Python dependencies..."
& (Join-Path $VectorAIDir "venv\Scripts\python.exe") -m pip install --upgrade pip --quiet
& (Join-Path $VectorAIDir "venv\Scripts\python.exe") -m pip install -r (Join-Path $VectorAIDir "requirements.txt") --quiet
# zeroconf is for the mDNS responder we run alongside chipper (Wire-Pod
# advertises only a service entry; Vector also needs an A-record for
# escapepod.local, which is what zeroconf provides).
& (Join-Path $VectorAIDir "venv\Scripts\python.exe") -m pip install zeroconf --quiet
Info "vector-ai ready."

# Note: the standalone mdns-responder.py and find-vector.py are no longer
# installed — the supervisor folds in both mDNS advertising and Vector IP
# rediscovery. They remain in shared/ only as manual diagnostic tools.

# ── escapepod.local in hosts file ─────────────────────────────────────────────
# So the browser running wpsetup.keriganc.com can poll Wire-Pod's local API
# during the Activate step. (Vector itself uses mDNS; this is for the browser.)
Step "Hosts file"
$hostsFile = "C:\Windows\System32\drivers\etc\hosts"
$content = [System.IO.File]::ReadAllText($hostsFile)
$lanIp = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" } | Select-Object -First 1).IPAddress
if ($content -notmatch "escapepod\.local") {
    $newContent = $content.TrimEnd() + "`r`n# Wire-Pod local resolution`r`n$lanIp`tescapepod.local`r`n"
    [System.IO.File]::WriteAllText($hostsFile, $newContent)
    Info "Added escapepod.local -> $lanIp to hosts file."
} else {
    Info "Hosts file already has an escapepod.local entry."
}
ipconfig /flushdns | Out-Null

# ── 6. Firewall ──────────────────────────────────────────────────────────────
Step "Windows Firewall"
# 443 = chipper gRPC/voice, 8080 = web UI, 80 = Vector's connCheck pings,
# 8084 = Vector 2.0.1 compatibility endpoint. Vector connects INBOUND to
# all of these — miss any and Vector shows the wifi-exclamation icon.
# Profile=Any so a Public-profile reclassification (common after a router
# reboot) doesn't silently block them.
foreach ($p in @(443, 8080, 80, 8084)) {
    $existing = Get-NetFirewallRule -DisplayName "VectorPod-$p" -ErrorAction SilentlyContinue
    if (-not $existing) {
        New-NetFirewallRule -DisplayName "VectorPod-$p" -Direction Inbound -Protocol TCP -LocalPort $p -Action Allow -Profile Any | Out-Null
        Info "Firewall: allowed TCP $p inbound (all profiles)."
    } else {
        Info "Firewall: TCP $p already allowed."
    }
}

# ── 7. Scheduled Task — the supervisor owns the whole stack ──────────────────
Step "Scheduled task"
$VectorAIExe = Join-Path $VectorAIDir "venv\Scripts\python.exe"

# supervisor.py replaces the old three-task sprawl (chipper / vector-ai /
# mDNS) plus find-vector.py and all manual recovery. It launches and keeps
# alive Ollama, chipper and vector-ai, advertises escapepod.local over mDNS,
# and auto-recovers from link drops, PC sleep, and Vector IP changes.
Copy-Item "$SharedDir\supervisor.py" (Join-Path $InstallRoot "supervisor.py") -Force
$SupervisorPy = Join-Path $InstallRoot "supervisor.py"

# One task. S4U = runs without an interactive desktop, so no console window
# ever appears for the supervisor or the children it spawns. RunLevel=Highest
# because the chipper child must bind privileged port 443. No trigger — only
# the start scripts start it.
$action    = New-ScheduledTaskAction -Execute $VectorAIExe -Argument "`"$SupervisorPy`"" -WorkingDirectory $InstallRoot
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$settings.ExecutionTimeLimit = "PT0S"   # unlimited
Register-ScheduledTask -TaskName "VectorPod-Supervisor" -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Info "Scheduled task VectorPod-Supervisor registered (S4U, hidden, manual-start only)."

# ── 8. Pull models ────────────────────────────────────────────────────────────
Step "Models"
Info "Pulling gemma3:12b — the main conversational model (~8 GB first time)..."
& ollama pull gemma3:12b
# Small, fast model used only for background conversation summaries; kept
# separate so summary calls never disturb the main model's prompt cache.
Info "Pulling llama3.2:3b — background conversation-summary model (~2 GB)..."
& ollama pull llama3.2:3b

# ── 9. Done ───────────────────────────────────────────────────────────────────
Step "Installation complete"
Write-Host ""
Write-Host "Daily use:" -ForegroundColor Green
Write-Host "  start-vector.ps1   # bring everything up"
Write-Host "  stop-vector.ps1    # shut everything down, free VRAM"
Write-Host ""
Write-Host "First-time Wire-Pod setup (one time only):" -ForegroundColor Yellow
Write-Host "  1. Run start-vector.ps1"
Write-Host "  2. Open http://localhost:8080 in your browser"
Write-Host "  3. Complete the wizard: server IP, English STT (downloads VOSK model)"
Write-Host "  4. Run:  .\apply-wirepod-config.ps1"
Write-Host "  5. Pair Vector via the Robots tab"
Write-Host ""
