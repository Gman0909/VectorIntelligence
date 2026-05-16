# Vector Robot AI Stack — Single-Machine Deployment

Turn an Anki/DDL Vector robot into a local, private, LLM-powered companion.
Everything runs on one machine — no cloud, no subscription.

```
                    ┌─────────────────────────────────────────┐
[Vector] ◄────────► │  VectorPod-Supervisor (one process)      │
   wake word,       │                                          │
   voice, camera    │   ├─ chipper      :443   Wire-Pod — voice │
                    │   ├─ vector-ai    :8000  LLM glue + memory│
                    │   ├─ Ollama       :11434 gemma4:e4b       │
                    │   └─ mDNS                escapepod.local  │
                    │                                          │
                    │   monitors all of the above, and         │
                    │   auto-recovers from WiFi drops, PC       │
                    │   sleep, and Vector IP changes            │
                    └─────────────────────────────────────────┘
```

A **supervisor** process owns the whole stack. It launches and keeps alive
Wire-Pod (the robot-facing voice server), `vector-ai` (a Python service that
wires Wire-Pod to the LLM and adds memory, vision and personality), and
Ollama (the local model runtime). It also advertises Vector's server over
mDNS and self-heals from the things that break a robot-on-WiFi setup.

Tested on Windows 10/11 and Debian-family Linux.

---

## What's in this bundle

```
VectorDeploy/
├── README.md
├── shared/                          assets identical on both platforms
│   ├── supervisor.py                the one process that owns the stack
│   ├── vector-ai/{service.py, memory.py, requirements.txt, .env}
│   ├── config/{wirepod-apiConfig.json, wirepod-intents-en-US.json,
│   │           vector-supervisor.service}
│   └── patches/                     source patches applied to Wire-Pod
├── windows/
│   ├── install.ps1 / install.cmd            one-time setup
│   ├── initial-setup.ps1                    drives Wire-Pod first-run wizard
│   ├── apply-wirepod-config.ps1             applies the AI config
│   ├── start-vector.cmd / stop-vector.cmd   daily use (double-click)
│   └── ...
└── linux/
    ├── install.sh
    ├── apply-wirepod-config.sh
    └── start-vector.sh / stop-vector.sh
```

---

## Hardware requirements

- **GPU**: ~10 GB VRAM free for `gemma4:e4b` (tested on RTX 4080 Super)
- **Disk**: ~18 GB free (Wire-Pod + Whisper.cpp build, Ollama + Whisper models)
- **OS**: Windows 10/11 x64, or Debian/Ubuntu/Mint x64 / ARM64
- **Network**: Vector and the host on the **same LAN**, ideally good 2.4GHz
  WiFi for Vector (he is 2.4GHz-only — a weak link is the #1 cause of trouble)

---

# Windows install

### One-time setup

1. **Open PowerShell as Administrator** in this folder, run:
   ```powershell
   .\windows\install.ps1
   ```
   Installs (via `winget`) anything missing — Go, Python, Git, MSYS2/mingw,
   Ollama — then clones and builds Wire-Pod (VOSK + Whisper chipper builds),
   builds whisper.cpp, sets up the `vector-ai` venv, registers the single
   **VectorPod-Supervisor** scheduled task, opens the firewall (443/8080/80/8084),
   and pulls `gemma4:e4b`. First run: 15–25 minutes, mostly downloads.

2. **Bring the stack up:** double-click `windows\start-vector.cmd`.

3. **Run the first-run wizard:**
   ```powershell
   .\windows\initial-setup.ps1
   ```
   Sets escape-pod mode so Vector pairs locally, downloads the STT model,
   generates SSL certs.

4. **Apply the AI config** (personality, vision rules, command vocabulary):
   ```powershell
   .\windows\apply-wirepod-config.ps1
   ```

5. **Enrol Vector** via the **Robots** tab at http://localhost:8080. The
   supervisor advertises `escapepod.local` so Vector finds the server.

### Daily use

Double-click **`start-vector.cmd`** / **`stop-vector.cmd`** — that's it.
Each is one `schtasks` call: start/stop the supervisor. Stopping it frees
VRAM (the model unloads). `.ps1` versions exist too if you prefer them.

---

# Linux install

```bash
cd linux && bash install.sh
```
Installs deps, builds Wire-Pod + whisper.cpp, grants chipper
`cap_net_bind_service` (so it binds :443 without root), registers
`vector-supervisor.service`. Then `bash start-vector.sh`, run the web-UI
wizard at `http://<ip>:8080`, `bash apply-wirepod-config.sh`, enrol Vector.

Daily use: `bash start-vector.sh` / `bash stop-vector.sh`.

---

## The supervisor

`supervisor.py` is the heart of the deployment. One process that:

- **launches and keeps alive** Ollama, chipper, and vector-ai — restarts any
  that die
- **advertises `escapepod.local`** over mDNS so Vector can find the server
- **auto-recovers** from the failure modes a robot-on-WiFi actually hits:
  - *Vector WiFi-link drop* — reconnects chipper the moment the link returns
  - *PC wake-from-sleep* — refreshes mDNS, re-asserts routing, bounces chipper
  - *Vector IP drift* — rediscovers him by mDNS, rewrites the stored IP
  - *LAN route hijack* — re-asserts a direct route if a VPN/Tailscale grabs it
- **clean shutdown** — children are bound to it (Windows job object / Linux
  cgroup), so stopping the supervisor reliably takes the whole stack down

Logs: `~/vector-pod/supervisor.log` (plus `chipper.log`, `vector-ai.log`).

---

## Speech-to-text

The installer builds two chipper binaries — VOSK and Whisper. **Whisper
`base.en`** is the default: far better than VOSK at names/accents, ~0.5s per
utterance on CPU. The model is set by the `WHISPER_MODEL`/`STT_SERVICE`
constants near the top of `supervisor.py` (`base.en` ↔ `small.en` for more
accuracy at ~1.5s/utterance).

## The LLM

Default is **`gemma4:e4b`** (Google Gemma 4, edge MoE — fast, multimodal).
`OLLAMA_MODEL` in `vector-ai/.env` selects it. The model auto-unloads after
idle to free VRAM and reloads on the next query.

## What the patches do

The installer patches Wire-Pod's source before building. Highlights:

| Patch | Effect |
|---|---|
| VAD `inactiveNumMax := 75` | ~1.5s silence window — pause mid-sentence without being cut off |
| `expand-animations.py` | +11 animations the LLM can use (fistBump, hello, dance, …) |
| `wake-word-grace-period.py` / `-mute-during-getimage.py` | Vector's own speech/shutter doesn't self-interrupt |
| `remove-photo-countdown.py` | photo capture is silent — no 3-2-1 theatrics |
| `prelim-lookatme-then-llm.py` | on vision queries Vector rapid-turns to face you |
| `slow-tts.py` | TTS cadence tuned for intelligibility |
| `add-eye-color-cmd.py` | LLM can shift Vector's eye colour to match mood |
| `add-sensor-reactions.py` | in-character reactions to pickup / putdown / pets |
| `add-button-interrupt.py` | the back button stops Vector mid-sentence |
| `add-ondemand-face.py` | Vector identifies who he's talking to (for per-face memory) only during a conversation — never a 24/7 face-detection stream |
| `fix-connection-leak.py` + patched `vector-go-sdk` | adds a `Close()` so every voice query releases its gRPC connection to Vector — without it the robot's SDK wedges after a few questions |
| minimal `en-US.json` | only come-here / charger / sleep / dance / "remember my face" stay as built-in intents — everything else goes to the LLM |

`apiConfig.json` carries the personality prompt (Marvin × Bender × Stephen Fry).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Vector unresponsive / wifi-exclamation | Check `supervisor.log` — it auto-recovers most drops. Persistent: it's almost always Vector's **2.4GHz WiFi link** — measure with `ping <vector-ip> -n 50`; a healthy LAN link is <5ms with no loss. Move him near the router; pick a clear 2.4GHz channel. |
| Voice stops after a router reboot | Windows may reclassify the network "Public", blocking inbound ports. `install.ps1` opens 443/8080/80/8084 `Profile=Any`; if it recurs, `Set-NetConnectionProfile -NetworkCategory Private`. |
| Vector reachable but traffic is slow/jittery | A VPN/Tailscale may be advertising a route for your LAN subnet. `Find-NetRoute -RemoteIPAddress <vector-ip>` — if it's not your Ethernet interface, the supervisor re-asserts a direct `/32` route; or set `--accept-routes=false` on Tailscale. |
| "Having trouble thinking" | `stop-vector` then `start-vector`. Check Ollama is up and the model is pulled. |
| Vector hallucinates instead of using his camera | Confirm `service.py` is current — the vision-intent regex must match the phrasing. |
| First reply after idle is slow | Expected — the model cold-loads into VRAM (~5s). It auto-unloads when idle to keep VRAM free. |

**Logs:** `~/vector-pod/supervisor.log`, `chipper.log`, `vector-ai.log`
(Linux: also `journalctl -u vector-supervisor -f`).
