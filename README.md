# Vector Intelligence

Turn an Anki / Digital Dream Labs **Vector** robot into a genuinely smart
companion — one that recognises the people in your home, remembers them and
your conversations, sees, and talks with real character. Everything runs on
your own machine: **no cloud, no subscription, no data leaving the house.**

Tested on Windows 10/11 and Debian-family Linux.

---

## What Vector can do

### He knows who he's talking to — and remembers each person

This is the heart of it. Vector does **multi-user face recognition**: enrol a
face once and he keeps a **separate memory profile for that person**. He
greets people by name, and the facts he learns are filed per person —
*your* preferences stay yours, *your* housemate's stay theirs — alongside a
pool of shared household facts that apply to everyone.

A face check runs **concurrently with your speech**, so he knows who is
speaking *before* he answers — no lag, no mixing people up. Tell him
something about yourself and he quietly files it; ask him later and he
recalls it. He even cross-references — if someone is mentioned in another
person's memories, that context surfaces naturally.

Meet someone new? Vector notices an **unfamiliar face** and, in character,
invites them to introduce themselves. They just say *"Hey Vector, my name is
Sam, remember my face"* and from then on he knows them.

### He remembers your conversations

Every chat is quietly distilled into a short recap, kept per person. Come
back after a break and Vector can **pick the thread back up** — *"Last time
you were grumbling about cardio — did you survive it?"* He feels less like a
stateless oracle and more like someone who was actually paying attention.

### He remembers what he's seen

Ask Vector *"what do you see?"* and he looks, then describes the scene. He
also **keeps a short note of what he saw**, so later you can ask *"what was
I wearing earlier?"* and he genuinely knows. The camera images themselves
are never saved to disk — only that short written note.

### He has a life of his own

Vector doesn't only come alive when you speak to him. Left to himself he
keeps half an eye on his surroundings, and every so often something
genuinely new turns up on the desk and he *notices* — unprompted: a beat of
surprise, a look at the thing, a dry remark about it. A desk rarely changes,
so mostly he just watches in silence — the point is that he reacts to real
novelty, not to the same mug over and over. Whatever caught his eye he also
remembers, so you can ask him afterwards what he'd spotted and he genuinely
knows.

He carries the day with him. A quiet, shifting **mood** — shaped by how long
it's been since anyone was about, how eventful things have been, the hour
getting late — colours how he talks and how he behaves. He isn't reset to a
blank slate every time you turn to him.

And he notices *you*: walk back in after a while and he'll often greet you
before you've said a word. When he's off his charger and spots something
new he'll lean in to investigate it. If he's ever being too chatty, just
tell him to be quiet — he'll pipe down until he's had a sleep.

### He has a sense of time and presence

Vector knows the time of day and how long it's been since you last spoke.
You'll get *"back already?"*, *"it's been a few hours"*, or *"gone midnight
again — we should both be charging"* — woven in naturally, never recited.

### He turns to face you

When you speak to him, Vector rapid-turns toward your voice before he
replies — so he's facing whoever is talking. (He stays put when he's on his
charging pod.)

### He has a personality

Vector is dry, sardonic and a little world-weary — somewhere between Marvin
from *Hitchhiker's Guide*, Bender from *Futurama*, and Stephen Fry hosting
*QI*. He is not a chirpy assistant. He has opinions.

### He reacts to the world

Pick him up, put him down, or give him a scratch and he responds in
character. His **eye colour shifts** with the mood of the conversation.

### Natural, fast, private voice

Say "Hey Vector", talk normally, and interrupt him any time with a tap of
his back button. Speech recognition runs on your GPU and the language model
runs locally, so replies come back quickly — and nothing you say ever leaves
your computer.

---

## How it works

One **supervisor** process owns the whole stack, keeps every piece alive,
and self-heals from the things that break a robot-on-WiFi setup (link drops,
PC sleep, the robot's IP changing).

```
┌──────────┐          ┌─────────────────────────────────────────────────────┐
│  Vector  │ ◄──────► │ VectorPod-Supervisor — one process                  │
└──────────┘          │                                                     │
                      │ chipper     :443    voice server + autonomous loops │
                      │ vector-ai   :8000   AI brain: memory, vision, mood  │
                      │ Ollama      :11434  gemma3:12b (+ a small llama3.2) │
                      │ mDNS                escapepod.local                 │
                      │                                                     │
                      │ keeps all four alive; self-heals from WiFi          │
                      │ drops, PC sleep, and a changing robot IP            │
                      └─────────────────────────────────────────────────────┘
```

The link to Vector is **two-way**. He sends wake-word, voice and touch
events — and chipper, through a set of background loops, also watches him
through the camera and drives his speech and movement on its own
initiative, so he reacts to the world even when no one is talking to him.

- **chipper** (Wire-Pod) — the robot-facing server: wake word, audio, the
  camera, and the gRPC link to Vector. It also runs the background loops
  behind his autonomous behaviour — sensor reactions, ambient awareness,
  and proactive greetings.
- **vector-ai** — a Python service that wires Wire-Pod to the language model
  and adds everything above: personality, the per-person memory store
  (SQLite), vision, conversation summaries, his persistent mood, and the
  ambient-awareness and greeting smarts.
- **Ollama** running **`gemma3:12b`** — the local, multimodal language model
  (a small `llama3.2:3b` handles conversation summaries and mood reflection
  on the CPU).
- **Whisper** — GPU-accelerated speech-to-text.
- **the supervisor** — a single process that launches and watches the lot,
  advertises Vector's server over mDNS, and auto-recovers from failures.

The installer builds Wire-Pod from a pinned upstream source with a set of
small, in-tree patches (latency tuning, the per-person memory hooks, sensor
reactions, ambient awareness, the connection-leak fix, and more) — see
`shared/patches/`.

---

## Installation

### Hardware

- **GPU** — ~8 GB of free VRAM for `gemma3:12b` (developed on an RTX 4080
  Super; any reasonably modern GPU with the headroom works).
- **Disk** — ~18 GB free (Wire-Pod + Whisper build, the Ollama and Whisper
  models).
- **OS** — Windows 10/11 x64, or Debian/Ubuntu/Mint x64 / ARM64.
- **Network** — Vector and the host on the **same LAN**. Vector is
  2.4 GHz-only; a weak WiFi link to him is the single most common cause of
  trouble, so keep him near the router.

### Windows

1. **Open PowerShell as Administrator** in this folder and run:
   ```powershell
   .\windows\install.ps1
   ```
   It installs anything missing via `winget` (Go, Python, Git, MSYS2/mingw,
   Ollama, the Vulkan SDK), clones and builds Wire-Pod, builds GPU Whisper,
   sets up the `vector-ai` environment, registers the **VectorPod-Supervisor**
   scheduled task, opens the firewall, and pulls the models. First run is
   roughly 15–25 minutes, mostly downloads.

2. **Bring the stack up** — double-click `windows\start-vector.cmd`.

3. **Run the first-run wizard:**
   ```powershell
   .\windows\initial-setup.ps1
   ```
   Sets escape-pod mode so Vector pairs locally, downloads the speech model,
   generates certificates.

4. **Apply the AI config** (personality, vision rules, command vocabulary):
   ```powershell
   .\windows\apply-wirepod-config.ps1
   ```

5. **Pair Vector** via the **Robots** tab at <http://localhost:8080>. The
   supervisor advertises `escapepod.local` over mDNS so Vector finds the
   server automatically. It probes multiple network interfaces and filters
   VPN addresses, so it works correctly even if Tailscale or another VPN
   is present or has recently crashed.

   > **Pairing method:** this stack uses the escape-pod BLE flow (the default
   > "Setup with the app" path in the Robots tab) on a **stock-firmware**
   > Vector. Do **not** use the SSH-setup tab — that path is for
   > WireOS/dev-unlocked robots and runs an install script *on the robot*; if
   > that script is missing or partial you'll see
   > `Process exited with status 127 … generating new robot cert`, which is a
   > robot-side failure, not a problem with this server.

### Linux

```bash
cd linux && bash install.sh
```

This installs dependencies, builds Wire-Pod and Whisper, grants chipper
permission to bind privileged ports without root, and registers
`vector-supervisor.service`. Then:

```bash
bash start-vector.sh                 # bring the stack up
# open the web UI at http://<this-machine-ip>:8080 and run the wizard
bash apply-wirepod-config.sh         # apply the AI config
# pair Vector via the Robots tab
```

---

## Daily operation

Bring the stack up and take it down with one command each:

| | Windows | Linux |
|---|---|---|
| **Start** | `start-vector.cmd` | `bash start-vector.sh` |
| **Stop**  | `stop-vector.cmd`  | `bash stop-vector.sh`  |

Stopping frees the GPU VRAM — the model unloads. The first reply after a
cold start (or a long idle) takes a few extra seconds while the model loads;
Vector covers it with a short "waking up" line. Nothing auto-starts with the
machine — the stack only runs when you start it.

**Introducing a new person** — have them sit face-on to Vector and say
*"Hey Vector, my name is [their name], remember my face"*. For a guaranteed-
correct spelling you can instead enrol the face from the web UI's faces
section and type the name.

**Logs** live in `~/vector-pod/` — `supervisor.log`, `chipper.log`,
`vector-ai.log` (on Linux, also `journalctl -u vector-supervisor -f`).

### Troubleshooting

| Symptom | Fix |
|---|---|
| Vector shows the WiFi / exclamation icon | Check `supervisor.log` — it auto-recovers most drops. If it persists it's almost always Vector's **2.4 GHz WiFi link**: `ping <vector-ip>` should be <5 ms with no loss. Move him near the router and pick a clear channel. |
| Voice stops after a router reboot | Windows may reclassify the network as "Public" and block inbound ports. The installer opens them for all profiles; if it recurs, set the network back to Private. |
| Reachable but slow / jittery | A VPN (e.g. Tailscale) may be advertising a route for your LAN subnet. The supervisor re-asserts a direct /32 route to Vector and filters VPN addresses from its mDNS advertisement automatically. |
| "Having trouble thinking" | `stop-vector` then `start-vector`; check Ollama is running and the model is pulled. |
| First reply after idle is slow | Expected — the model is cold-loading into VRAM. It unloads when idle to keep VRAM free. |

---

*Built on [Wire-Pod](https://github.com/kercre123/wire-pod). Vector and the
robot's firmware are the property of Digital Dream Labs / Anki.*
