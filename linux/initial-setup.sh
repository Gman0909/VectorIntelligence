#!/usr/bin/env bash
# initial-setup.sh — Drive Wire-Pod's first-run wizard via REST.
# Runs ONCE after install.sh + wire-pod first start.
# Idempotent.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[X]${NC} $*" >&2; exit 1; }

# Web UI port chosen at install time (pod.conf next to supervisor.py).
WEB_PORT=8080
POD_CONF="$HOME/vector-pod/pod.conf"
if [ -f "$POD_CONF" ]; then
    PORT_FROM_CONF=$(sed -n 's/^[[:space:]]*WEB_PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$POD_CONF" | head -1 || true)
    [ -n "${PORT_FROM_CONF:-}" ] && WEB_PORT="$PORT_FROM_CONF"
fi
BASE="http://localhost:$WEB_PORT"

curl -s --connect-timeout 3 "$BASE" >/dev/null \
    || fail "Wire-Pod web UI not responding on $WEB_PORT. Run: sudo systemctl start wire-pod"

info "Setting STT language to en-US..."
resp=$(curl -s -X POST "$BASE/api/set_stt_info" \
       -H 'Content-Type: application/json' -d '{"language":"en-US"}')

if echo "$resp" | grep -q "downloading"; then
    info "Downloading VOSK English model (~50 MB)..."
    while true; do
        sleep 5
        status=$(curl -s "$BASE/api/get_download_status")
        echo "    status: $status"
        if echo "$status" | grep -q "success";         then info "VOSK installed."; break; fi
        if echo "$status" | grep -q "error";           then fail "VOSK download failed: $status"; fi
        if echo "$status" | grep -q "not downloading"; then break; fi
    done
fi

# Escape pod mode (not IP mode) — matches what wpsetup.keriganc.com expects
# for the Activate step. Vector gets handed `escapepod.local:443` as the
# server endpoint and resolves it via mDNS (avahi-daemon on Linux handles
# this natively from the system hostname).
info "Switching Wire-Pod to escape pod mode..."
curl -s --max-time 30 "$BASE/api-chipper/use_ep" >/dev/null \
    || fail "use_ep call failed"
sleep 3
info "Wire-Pod is now in escape pod mode."

info "Applying our AI config (personality + endpoint)..."
bash "$SCRIPT_DIR/apply-wirepod-config.sh"

# Linux normally broadcasts the system hostname over mDNS via avahi. If the
# machine's hostname isn't 'escapepod', Vector won't be able to resolve
# 'escapepod.local'. Add an avahi alias as a safety net.
if command -v avahi-publish >/dev/null 2>&1; then
    if [ "$(hostname)" != "escapepod" ]; then
        warn "Your hostname is '$(hostname)', not 'escapepod'. Vector resolves Wire-Pod via 'escapepod.local'."
        warn "Two options:"
        warn "  1. Permanent: sudo hostnamectl set-hostname escapepod && sudo reboot"
        warn "  2. Per-session: avahi-publish -a -R escapepod.local \$(hostname -I | awk '{print \$1}')"
    fi
fi

echo ""
echo -e "${GREEN}Initial setup complete.${NC}"
echo "Next: factory-reset Vector and pair via http://$(hostname -I | awk '{print $1}'):${WEB_PORT} → Bot Setup tab."
