#!/usr/bin/env bash
# Bring Vector's stack up. The vector-supervisor service owns everything —
# it launches Ollama, chipper and vector-ai, advertises mDNS, and
# auto-recovers from drops/sleep/IP changes. So "start" is just that.
set -e
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }

if ! systemctl list-unit-files | grep -q '^vector-supervisor\.service'; then
    warn "vector-supervisor.service not found — run install.sh first."
    exit 1
fi

sudo systemctl start vector-supervisor.service
ok "Supervisor starting — bringing up Ollama, Wire-Pod and vector-ai."

sleep 18
if curl -s --max-time 5 http://127.0.0.1:8000/health >/dev/null 2>&1; then
    ok "vector-ai up."
else
    warn "vector-ai not up yet (supervisor will keep retrying)."
fi
echo ""
echo "Say 'Hey Vector' to chat. Stop with stop-vector.sh when done."
