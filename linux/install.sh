#!/usr/bin/env bash
# install.sh — Deploy Vector AI stack on a single Linux box (Debian/Ubuntu/Mint).
# Runs everything locally: Wire-Pod, vector-ai, Ollama. No second machine needed.
# Run as your regular user (with sudo access), NOT as root.

set -euo pipefail

WIREPOD_REPO="https://github.com/kercre123/wire-pod"
WIREPOD_DIR="$HOME/wire-pod"
VECTORAI_DIR="$HOME/vector-ai"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "$SCRIPT_DIR/../shared" && pwd)"
GO_VERSION="1.22.4"

# Pinned upstream commits — our patch scripts are written against these exact
# revisions. Bumping them is a deliberate, re-test-everything decision; never
# float to HEAD or a future upstream change will break the patches silently.
WIREPOD_COMMIT="11e7b22095166ed35765e88a8a10ed3a6ce49d5c"
WHISPER_COMMIT="60cd96acff3a72895cb9ae9cbabe9de21b1e9125"
SDK_COMMIT="62168f3595d67ae0bf24103a9fe1fc5f2eb9b85c"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
step()  { echo -e "\n${BOLD}── $* ${NC}"; }
die()   { echo -e "${RED}[✗] $*${NC}" >&2; exit 1; }

[[ $EUID -eq 0 ]] && die "Do not run as root. Run as your regular user (sudo will be used where needed)."

# ── Optional arguments ────────────────────────────────────────────────────────
# --web-port N sets Wire-Pod's web UI / config-server port (default 8080). It's
# written to pod.conf so the supervisor and initial-setup.sh stay in agreement.
WEB_PORT=8080
WEB_PORT_SET=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --web-port)   WEB_PORT="${2:-}"; WEB_PORT_SET=true; shift 2 ;;
        --web-port=*) WEB_PORT="${1#*=}"; WEB_PORT_SET=true; shift ;;
        *) die "Unknown argument: $1 (supported: --web-port N)" ;;
    esac
done
[[ "$WEB_PORT" =~ ^[0-9]+$ ]] || die "--web-port must be numeric (got: '$WEB_PORT')."

# ── 1. System dependencies ────────────────────────────────────────────────────
step "System dependencies"
sudo apt-get update -qq
sudo apt-get install -y git python3-venv python3-pip curl unzip build-essential avahi-daemon

# ── 2. Go ─────────────────────────────────────────────────────────────────────
step "Go toolchain"
NEED_GO=true
if command -v go &>/dev/null; then
    CURRENT_GO=$(go version | grep -oP '\d+\.\d+' | head -1)
    if awk "BEGIN{exit !($CURRENT_GO >= 1.21)}"; then
        info "Go $CURRENT_GO already installed."
        NEED_GO=false
    fi
fi
if $NEED_GO; then
    ARCH=$(dpkg --print-architecture)
    case "$ARCH" in
        arm64) GO_ARCH="arm64" ;;
        armhf) GO_ARCH="armv6l" ;;
        amd64) GO_ARCH="amd64" ;;
        *) die "Unsupported architecture: $ARCH" ;;
    esac
    info "Installing Go ${GO_VERSION} (${GO_ARCH})..."
    curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${GO_ARCH}.tar.gz" -o /tmp/go.tar.gz
    sudo rm -rf /usr/local/go
    sudo tar -C /usr/local -xzf /tmp/go.tar.gz
    rm /tmp/go.tar.gz
    if ! grep -q '/usr/local/go/bin' ~/.bashrc; then
        echo 'export PATH="/usr/local/go/bin:$PATH"' >> ~/.bashrc
    fi
fi
export PATH="/usr/local/go/bin:$PATH"
info "Using $(go version)"

# ── 3. Ollama + model ─────────────────────────────────────────────────────────
step "Ollama"
if ! command -v ollama &>/dev/null; then
    info "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
else
    info "Ollama already installed."
fi

info "Ensuring gemma3:12b is present (the main conversational model)..."
ollama pull gemma3:12b
# Small, fast model used only for background conversation summaries; kept
# separate so summary calls never disturb the main model's prompt cache.
info "Ensuring llama3.2:3b is present (background conversation-summary model)..."
ollama pull llama3.2:3b

# ── 4. Wire-Pod ───────────────────────────────────────────────────────────────
step "Wire-Pod"
if [ ! -d "$WIREPOD_DIR/.git" ]; then
    info "Cloning Wire-Pod..."
    git clone "$WIREPOD_REPO" "$WIREPOD_DIR"
fi
# Pin to the exact commit our patches are written against.
info "Checking out pinned Wire-Pod commit..."
git -C "$WIREPOD_DIR" fetch -q origin "$WIREPOD_COMMIT" 2>/dev/null || true
git -C "$WIREPOD_DIR" checkout -q "$WIREPOD_COMMIT" || die "Could not check out pinned Wire-Pod commit $WIREPOD_COMMIT."

info "Minimising Wire-Pod intents (only 'come here' + 'go home', everything else to LLM)..."
INTENT_FILE="$WIREPOD_DIR/chipper/intent-data/en-US.json"
if [ -f "$INTENT_FILE" ] && [ ! -f "$INTENT_FILE.backup" ]; then
    sudo cp "$INTENT_FILE" "$INTENT_FILE.backup"
fi
sudo cp "$SHARED_DIR/config/wirepod-intents-en-US.json" "$INTENT_FILE"

info "Patching Wire-Pod listening timeout (~460ms -> 1.5s of silence)..."
VAD_FILE="$WIREPOD_DIR/chipper/pkg/wirepod/speechrequest/speechrequest.go"
if grep -qE 'inactiveNumMax := (23|150|100|75)' "$VAD_FILE"; then
    sed -i -E 's|inactiveNumMax := (23\|150\|100\|75)[^\r\n]*|inactiveNumMax := 75 // 1.5s of silence|' "$VAD_FILE"
    info "Patch applied (1.5s)."
else
    warn "VAD line not found in $VAD_FILE — Wire-Pod source may have changed."
fi

info "Expanding Wire-Pod animation vocabulary..."
sudo python3 "$SHARED_DIR/patches/expand-animations.py" "$WIREPOD_DIR/chipper/pkg/wirepod/ttr/kgsim_cmds.go"

info "Adding wake-word interrupt grace period..."
sudo python3 "$SHARED_DIR/patches/wake-word-grace-period.py" "$WIREPOD_DIR/chipper/pkg/wirepod/ttr/kgsim_interrupt.go"

info "Making the back button interrupt Vector's speech..."
sudo python3 "$SHARED_DIR/patches/add-button-interrupt.py" "$WIREPOD_DIR/chipper/pkg/wirepod/ttr/kgsim_interrupt.go"

info "Muting wake-word interrupts during getImage (prevents shutter-sound self-interrupt)..."
sudo python3 "$SHARED_DIR/patches/wake-word-mute-during-getimage.py" "$WIREPOD_DIR"

info "Adding on-demand face detection (per-interaction only, never a 24/7 firehose)..."
sudo python3 "$SHARED_DIR/patches/add-ondemand-face.py" "$WIREPOD_DIR/chipper/pkg/wirepod/ttr/kgsim_interrupt.go"

info "Removing photo viewfinder + 3-2-1 countdown (shutter animation stays — it's our audio cue)..."
sudo python3 "$SHARED_DIR/patches/remove-photo-countdown.py" "$WIREPOD_DIR/chipper/pkg/wirepod/ttr/kgsim_cmds.go"

info "Routing 'dance' and 'lookAtUser' aliases to Vector's built-in behaviours..."
sudo python3 "$SHARED_DIR/patches/use-builtin-behaviors.py" "$WIREPOD_DIR/chipper/pkg/wirepod/ttr/kgsim_cmds.go"

info "On vision queries, dispatch intent_imperative_lookatme BEFORE the LLM so Vector rapid-turns using his fresh mic-direction cache..."
sudo python3 "$SHARED_DIR/patches/prelim-lookatme-then-llm.py" "$WIREPOD_DIR/chipper/pkg/wirepod/preqs/intent_graph.go"

info "Slowing Vector's TTS slightly for clarity..."
sudo python3 "$SHARED_DIR/patches/slow-tts.py" "$WIREPOD_DIR/chipper/pkg/wirepod/ttr/kgsim_cmds.go"

info "Adding LLM-driven eye colour command (mood expression)..."
sudo python3 "$SHARED_DIR/patches/add-eye-color-cmd.py" "$WIREPOD_DIR/chipper/pkg/wirepod/ttr/kgsim_cmds.go"

info "Adding background sensor reactions (pickup, putdown, pet)..."
sudo python3 "$SHARED_DIR/patches/add-sensor-reactions.py" "$WIREPOD_DIR"

info "Fixing the gRPC connection leak (defer robot.Close() in kgsim.go)..."
sudo python3 "$SHARED_DIR/patches/fix-connection-leak.py" "$WIREPOD_DIR/chipper/pkg/wirepod/ttr/kgsim.go"

info "Fixing name parsing so face enrollment captures just the name..."
sudo python3 "$SHARED_DIR/patches/fix-name-extraction.py" "$WIREPOD_DIR/chipper/pkg/wirepod/ttr/intentparam.go"

info "Adding the concurrent face probe (knows the speaker before the LLM replies)..."
sudo python3 "$SHARED_DIR/patches/add-face-probe.py" "$WIREPOD_DIR"

info "Adding ambient awareness (idle novelty observation loop)..."
sudo python3 "$SHARED_DIR/patches/add-ambient-loop.py" "$WIREPOD_DIR"

# Patched vector-go-sdk: upstream opens a gRPC connection per vector.New()
# but never closes it, so every voice query leaks one until the robot's SDK
# wedges. Pull the pinned SDK commit into chipper/third_party, patch in a
# Close() method, and point chipper's go.mod at the local copy.
info "Installing patched vector-go-sdk (adds Close() to stop the leak)..."
SDK_DIR="$WIREPOD_DIR/chipper/third_party/vector-go-sdk"
if [ ! -f "$SDK_DIR/go.mod" ]; then
    mkdir -p "$(dirname "$SDK_DIR")"
    git clone "https://github.com/fforchino/vector-go-sdk" "$SDK_DIR"
    git -C "$SDK_DIR" fetch -q origin "$SDK_COMMIT" 2>/dev/null || true
    git -C "$SDK_DIR" checkout -q "$SDK_COMMIT" || die "Could not check out pinned vector-go-sdk commit $SDK_COMMIT."
    # Drop .git — this is now a vendored local module, not a clone to update.
    rm -rf "$SDK_DIR/.git"
fi
# add-sdk-close.py is idempotent — safe to run on every install.
sudo python3 "$SHARED_DIR/patches/add-sdk-close.py" "$SDK_DIR/pkg/vector/vector.go"
CHIPPER_GOMOD="$WIREPOD_DIR/chipper/go.mod"
if ! grep -q 'replace github.com/fforchino/vector-go-sdk' "$CHIPPER_GOMOD"; then
    printf '\nreplace github.com/fforchino/vector-go-sdk => ./third_party/vector-go-sdk\n' \
        | sudo tee -a "$CHIPPER_GOMOD" > /dev/null
    info "go.mod now points at the patched vector-go-sdk."
fi

info "Building Wire-Pod chipper (VOSK, ~1 min)..."
cd "$WIREPOD_DIR/chipper"
CGO_LDFLAGS="-L/usr/local/lib" LD_LIBRARY_PATH=/usr/local/lib go build -o chipper ./cmd/vosk
info "Wire-Pod (VOSK) built OK."
cd "$SCRIPT_DIR"

# ── 4b. Whisper.cpp STT (better accuracy than VOSK) ──────────────────────────
step "Whisper.cpp STT"
WHISPER_REPO="$WIREPOD_DIR/whisper.cpp"
if [ ! -d "$WHISPER_REPO/.git" ]; then
    info "Cloning whisper.cpp..."
    git clone https://github.com/kercre123/whisper.cpp.git "$WHISPER_REPO"
fi
info "Checking out pinned whisper.cpp commit..."
git -C "$WHISPER_REPO" fetch -q origin "$WHISPER_COMMIT" 2>/dev/null || true
git -C "$WHISPER_REPO" checkout -q "$WHISPER_COMMIT" || die "Could not check out pinned whisper.cpp commit $WHISPER_COMMIT."
sudo apt-get install -y cmake
if [ ! -f "$WHISPER_REPO/build_go/src/libwhisper.so" ] && [ ! -f "$WHISPER_REPO/build_go/src/libwhisper.dylib" ]; then
    info "Building libwhisper (CPU, ~2-3 min)..."
    cd "$WHISPER_REPO"
    cmake -B build_go -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DWHISPER_BUILD_EXAMPLES=OFF -DWHISPER_BUILD_TESTS=OFF
    cmake --build build_go --config Release -j 4
    cd "$SCRIPT_DIR"
fi
# base.en default — ~2x faster than small.en on CPU, accuracy still well
# above VOSK. Switch via STT_SERVICE/WHISPER_MODEL if you want small.en.
WHISPER_MODEL_FILE="$WHISPER_REPO/models/ggml-base.en.bin"
if [ ! -f "$WHISPER_MODEL_FILE" ]; then
    info "Downloading Whisper base.en model (~142 MB)..."
    curl -L -o "$WHISPER_MODEL_FILE" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
fi
info "Building chipper-whisper (~1 min)..."
cd "$WIREPOD_DIR/chipper"
CGO_CFLAGS="-I$WHISPER_REPO/include -I$WHISPER_REPO/ggml/include" \
CGO_LDFLAGS="-L$WHISPER_REPO/build_go/src -L$WHISPER_REPO/build_go/ggml/src -lwhisper" \
LD_LIBRARY_PATH="$WHISPER_REPO/build_go/src:$WHISPER_REPO/build_go/ggml/src" \
go build -o chipper-whisper ./cmd/experimental/whisper.cpp
info "chipper-whisper built OK."

# Let chipper bind privileged ports (80, 443) without running as root, so
# the supervisor can run as the normal user.
sudo setcap 'cap_net_bind_service=+ep' "$WIREPOD_DIR/chipper/chipper-whisper"
info "Granted chipper cap_net_bind_service (binds :80/:443 without root)."
cd "$SCRIPT_DIR"

# ── 5. vector-ai Python service ───────────────────────────────────────────────
step "vector-ai Python service"
mkdir -p "$VECTORAI_DIR"
mkdir -p "$HOME/vector-pod"
cp "$SHARED_DIR/vector-ai/service.py"       "$VECTORAI_DIR/service.py"
cp "$SHARED_DIR/vector-ai/memory.py"        "$VECTORAI_DIR/memory.py"
cp "$SHARED_DIR/vector-ai/requirements.txt" "$VECTORAI_DIR/requirements.txt"
cp "$SHARED_DIR/supervisor.py"              "$HOME/vector-pod/supervisor.py"

# pod.conf — single source of truth for the web UI port, read by supervisor.py
# and initial-setup.sh. An explicit --web-port wins; otherwise preserve any
# value already there so re-running the installer won't clobber a manual edit.
POD_CONF="$HOME/vector-pod/pod.conf"
if ! $WEB_PORT_SET && [ -f "$POD_CONF" ]; then
    EXISTING=$(sed -n 's/^[[:space:]]*WEB_PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$POD_CONF" | head -1 || true)
    [ -n "${EXISTING:-}" ] && WEB_PORT="$EXISTING"
fi
echo "WEB_PORT=$WEB_PORT" > "$POD_CONF"
info "pod.conf written (WEB_PORT=$WEB_PORT)."

if [ ! -f "$VECTORAI_DIR/.env" ]; then
    cp "$SHARED_DIR/vector-ai/.env" "$VECTORAI_DIR/.env"
    info ".env copied — defaults to local Ollama on 127.0.0.1."
else
    warn ".env already exists — not overwriting."
fi

info "Creating Python venv..."
python3 -m venv "$VECTORAI_DIR/venv"
"$VECTORAI_DIR/venv/bin/pip" install -q --upgrade pip
"$VECTORAI_DIR/venv/bin/pip" install -q -r "$VECTORAI_DIR/requirements.txt"
# Verify the critical runtime deps actually landed — otherwise vector-ai
# crash-loops on "No module named uvicorn" and the supervisor just keeps
# restarting it. (set -e aborts on a failed pip, but not on pip exit 0 +
# a broken import, so check explicitly.)
"$VECTORAI_DIR/venv/bin/python" -c "import uvicorn, fastapi, httpx, zeroconf, dotenv, pydantic"
info "Python service ready."

# ── 6. Systemd service — one supervisor unit ─────────────────────────────────
step "Systemd service"
# One unit: vector-supervisor. It launches and keeps alive Ollama, chipper
# and vector-ai, advertises mDNS, and auto-recovers. KillMode=control-group
# means stopping the unit tears down every child too.
sed "s|__HOME__|$HOME|g; s|__USER__|$USER|g" \
    "$SHARED_DIR/config/vector-supervisor.service" \
    | sudo tee /etc/systemd/system/vector-supervisor.service > /dev/null
# Retire the old split units if a previous install left them.
sudo systemctl disable --now wire-pod.service vector-ai.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/wire-pod.service /etc/systemd/system/vector-ai.service
sudo systemctl daemon-reload
info "vector-supervisor.service installed (not started — use start-vector.sh)."

# ── 7. Summary ────────────────────────────────────────────────────────────────
LOCAL_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}${BOLD}══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  Installation complete!${NC}"
echo -e "${GREEN}${BOLD}══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}Next steps:${NC}"
echo ""
echo -e "  1. Bring the stack up:"
echo "       bash $SCRIPT_DIR/start-vector.sh"
echo ""
echo -e "  2. Open the web UI at http://${LOCAL_IP}:${WEB_PORT}"
echo "     Set the server IP to ${LOCAL_IP}, choose English STT, complete setup."
echo ""
echo -e "  3. Apply the AI config:"
echo "       bash $SCRIPT_DIR/apply-wirepod-config.sh"
echo ""
echo -e "  4. Enroll Vector via the web UI (Robots tab)."
echo ""
echo -e "  ${BOLD}Daily use:${NC}"
echo "       bash $SCRIPT_DIR/start-vector.sh   # bring everything up"
echo "       bash $SCRIPT_DIR/stop-vector.sh    # shut everything down"
echo ""
