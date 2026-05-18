#!/usr/bin/env bash
# apply-wirepod-config.sh — Apply AI settings to Wire-Pod after initial setup.
# Run this AFTER completing Wire-Pod's web UI setup (http://<pi-ip>:8080).
# It merges our AI config into Wire-Pod's apiConfig.json without wiping
# the SSL/enrollment fields that the setup UI wrote.

set -euo pipefail

WIREPOD_DIR="$HOME/wire-pod"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(cd "$SCRIPT_DIR/../shared" && pwd)"
CONFIG_SRC="$SHARED_DIR/config/wirepod-apiConfig.json"
CONFIG_DST="$WIREPOD_DIR/chipper/apiConfig.json"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }

if [ ! -f "$CONFIG_DST" ]; then
    warn "Wire-Pod config not found at $CONFIG_DST"
    warn "Has Wire-Pod initial setup been completed via the web UI?"
    exit 1
fi

# Merge our keys (knowledge, STT, weather) into the existing config,
# preserving server/cert fields that Wire-Pod wrote during initial setup.
python3 - <<PYEOF
import json, sys

with open("$CONFIG_SRC", encoding="utf-8") as f:
    our = json.load(f)
with open("$CONFIG_DST", encoding="utf-8") as f:
    live = json.load(f)

# Apply our sections; preserve anything Wire-Pod manages itself
for key in ("knowledge", "STT", "weather"):
    live[key] = our[key]

with open("$CONFIG_DST", "w", encoding="utf-8") as f:
    json.dump(live, f, indent=2, ensure_ascii=False)

print("Config merged OK.")
PYEOF

info "Restarting Wire-Pod..."
sudo systemctl restart wire-pod.service
sleep 2
sudo systemctl is-active wire-pod.service && info "Wire-Pod is running." || warn "Wire-Pod failed to start — check: journalctl -u wire-pod -n 30"
