#!/usr/bin/env python3
"""Vector Pod Supervisor — one process that owns the whole stack.

Replaces the three separate Scheduled Tasks (chipper / vector-ai / mDNS),
find-vector.py, and every manual restart-and-recover step.

It:
  - launches and keeps alive Ollama, chipper, and vector-ai
  - advertises escapepod.local over mDNS (folds in the old mdns-responder)
  - health-monitors everything and restarts whatever dies
  - auto-recovers from the failure modes this deployment actually hits:
      * Vector WiFi-link drop  -> reconnect chipper when the link returns
      * PC wake-from-sleep     -> refresh mDNS, re-assert route, bounce chipper
      * Vector IP drift        -> rediscover via mDNS, rewrite botSdkInfo
      * Tailscale/LAN route    -> re-assert a direct /32 route to Vector
  - on shutdown, stops the children and unloads the model to free VRAM

No hardcoded paths or IPs: every path is derived from this file's location,
Vector is found by mDNS, and the LAN IP is detected at runtime. Portable
between machines and (with the platform guards) Windows and Linux.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")

# ── Paths — all derived, nothing hardcoded ────────────────────────────────────
POD_DIR      = Path(__file__).resolve().parent          # .../vector-pod
WIREPOD_DIR  = POD_DIR / "wire-pod"
CHIPPER_DIR  = WIREPOD_DIR / "chipper"
VECTORAI_DIR = POD_DIR / "vector-ai"
BOTINFO      = CHIPPER_DIR / "jdocs" / "botSdkInfo.json"
SUP_LOG      = POD_DIR / "supervisor.log"
CHIPPER_LOG  = POD_DIR / "chipper.log"
VECTORAI_LOG = POD_DIR / "vector-ai.log"

# ── Tunables (the few things worth changing live here, not scattered) ─────────
STT_SERVICE   = "whisper"     # whisper | vosk
WHISPER_MODEL = "base.en"
HEALTH_PERIOD = 10            # seconds between health checks
SLEEP_GAP     = 60            # a tick gap longer than this == PC slept
LOG_MAX_BYTES = 5 * 1024 * 1024  # a log past this is rotated aside at startup

EXE = ".exe" if IS_WINDOWS else ""


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [supervisor] {msg}"
    print(line, flush=True)
    try:
        with open(SUP_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def rotate_log(path: Path):
    """If `path` is larger than LOG_MAX_BYTES, move it aside to <path>.old
    (replacing any previous .old). Called once at startup — so logs stay
    bounded across restarts without ever touching a file a running process
    still holds open."""
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            old = path.with_name(path.name + ".old")
            old.unlink(missing_ok=True)
            path.rename(old)
    except OSError:
        pass


# ── Small helpers ─────────────────────────────────────────────────────────────

_last_good_ip = None  # remembered so a post-wake detection failure can't
                      # silently downgrade mDNS to loopback

# ── IP-classification helpers ─────────────────────────────────────────────────
# Tailscale CGNAT range: 100.64.0.0/10  →  100.64.x.x – 100.127.x.x
_TS_LO = (100 << 24) | (64 << 16)
_TS_HI = (100 << 24) | (128 << 16)


def _ip_int(ip: str) -> int:
    try:
        a, b, c, d = ip.split(".")
        return (int(a) << 24) | (int(b) << 16) | (int(c) << 8) | int(d)
    except Exception:
        return 0


def _is_tailscale_ip(ip: str) -> bool:
    """True if ip sits in Tailscale's CGNAT range (100.64.0.0/10)."""
    n = _ip_int(ip)
    return _TS_LO <= n < _TS_HI


def _is_rfc1918(ip: str) -> bool:
    n = _ip_int(ip)
    return (
        ((10  << 24)               ) <= n < ((11  << 24)                ) or
        ((172 << 24) | (16 << 16) ) <= n < ((172 << 24) | (32 << 16)  ) or
        ((192 << 24) | (168 << 16)) <= n < ((192 << 24) | (169 << 16) )
    )


def _probe_local_ip(target: str) -> str | None:
    """UDP-connect trick: ask the OS which local IP it would use to reach target."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 1))
        ip = s.getsockname()[0]
        return ip if (ip and ip != "0.0.0.0"
                      and not ip.startswith("127.")
                      and not ip.startswith("169.254.")) else None
    except OSError:
        return None
    finally:
        s.close()


def _detect_local_ip(prefer_target: str = None) -> str | None:
    """Best-effort LAN IP, filtering out Tailscale and VPN addresses.

    Using a single probe to 10.255.255.255 fails when Tailscale crashes and
    leaves stale routing entries: the connect either errors (returning None,
    which then falls back to loopback) or resolves to the Tailscale CGNAT
    address (100.64-127.x.x), which Vector can't reach.

    Instead we try multiple probes and filter explicitly:
    • prefer_target (Vector's known LAN IP) — the most accurate probe because
      it finds the exact interface that routes to Vector.
    • 224.0.0.251 (mDNS multicast) — LAN-only; VPN tunnels don't carry
      multicast, so this probe can never return a VPN address.
    • Common LAN gateway IPs — tiebreakers for the case where neither of the
      above resolves."""
    # Highest-confidence: the interface that actually routes to Vector.
    if prefer_target:
        ip = _probe_local_ip(prefer_target)
        if ip and not _is_tailscale_ip(ip):
            return ip

    candidates: list[str] = []

    # mDNS multicast — the correct interface by definition.
    ip = _probe_local_ip("224.0.0.251")
    if ip and not _is_tailscale_ip(ip):
        candidates.append(ip)

    # Common gateway IPs as fallbacks.
    for target in ("192.168.1.1", "192.168.0.1", "10.0.0.1", "172.16.0.1"):
        ip = _probe_local_ip(target)
        if ip and not _is_tailscale_ip(ip):
            candidates.append(ip)

    # Prefer RFC-1918 private LAN addresses.
    for ip in candidates:
        if _is_rfc1918(ip):
            return ip
    return candidates[0] if candidates else None


def local_ip(wait: float = 0.0, prefer_target: str = None) -> str:
    """This machine's LAN IP for advertising escapepod.local.

    Filters Tailscale CGNAT addresses so mDNS is never published on a VPN
    interface. If prefer_target is given (e.g. Vector's known IP) we probe
    routing to it directly — the most accurate way to pick the right interface.
    Polls for up to `wait` seconds, falls back to the last known-good address,
    and only as a true last resort returns loopback."""
    global _last_good_ip
    deadline = time.monotonic() + wait
    while True:
        ip = _detect_local_ip(prefer_target)
        if ip:
            _last_good_ip = ip
            return ip
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    # Only fall back to last-good if it's still a plausible LAN address.
    if _last_good_ip and not _is_tailscale_ip(_last_good_ip):
        log(f"local_ip: network not ready — using last known-good {_last_good_ip}")
        return _last_good_ip
    log("local_ip: no LAN IP available and no known-good fallback — using loopback")
    return "127.0.0.1"


def http_ok(url: str, timeout: float = 4.0) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def tcp_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ── Windows job object — children die with the supervisor ────────────────────
# Without this, processes the supervisor spawns ORPHAN when it's stopped:
# Stop-ScheduledTask kills the supervisor but not its children, and since the
# supervisor runs elevated (chipper needs port 443) the children are elevated
# too — so a non-admin stop-vector can't kill them either. A job object with
# KILL_ON_JOB_CLOSE makes Windows terminate every child the moment the
# supervisor process exits, for any reason (clean stop, crash, kill).

_win_job = None  # HANDLE; held for the supervisor's lifetime


def create_win_job():
    global _win_job
    if not IS_WINDOWS:
        return
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IOCNT(ctypes.Structure):
        _fields_ = [("r", ctypes.c_uint64), ("w", ctypes.c_uint64),
                    ("o", ctypes.c_uint64), ("rt", ctypes.c_uint64),
                    ("wt", ctypes.c_uint64), ("ot", ctypes.c_uint64)]

    class EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC),
                    ("IoInfo", IOCNT),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    job = k32.CreateJobObjectW(None, None)
    if not job:
        log("WARNING: CreateJobObject failed — children may orphan on stop")
        return
    info = EXT()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                ctypes.byref(info), ctypes.sizeof(info))
    _win_job = job
    log("job object created (children will be killed with the supervisor)")


def assign_to_job(pid: int):
    if not IS_WINDOWS or not _win_job or not pid:
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100
    h = k32.OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, pid)
    if h:
        k32.AssignProcessToJobObject(_win_job, h)
        k32.CloseHandle(h)


def chipper_binary() -> Path:
    """Prefer the Whisper chipper; fall back to the VOSK one."""
    whisper = CHIPPER_DIR / f"chipper-whisper{EXE}"
    if whisper.exists():
        return whisper
    return CHIPPER_DIR / f"chipper{EXE}"


def read_vector_ip() -> str | None:
    try:
        data = json.loads(BOTINFO.read_text(encoding="utf-8-sig"))
        robots = data.get("robots") or []
        if robots:
            return robots[0].get("ip_address")
    except Exception:
        pass
    return None


# ── mDNS: advertise escapepod.local so Vector can find this server ───────────
# On Linux, avahi already publishes <hostname>.local — but advertising
# escapepod.local explicitly is harmless and keeps behaviour identical
# cross-platform.

class MDNS:
    def __init__(self):
        self.zc = None
        self.info = None
        self._advertised_ip: str | None = None

    def start(self, prefer_target: str = None, wait: float = 30.0) -> str:
        """Advertise escapepod.local. Returns the IP actually published.

        prefer_target is Vector's known LAN IP — passed to local_ip() so we
        probe routing to Vector directly and pick the interface that talks to
        him, regardless of whether Tailscale or another VPN is present."""
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
        ip = local_ip(wait=wait, prefer_target=prefer_target)
        self._advertised_ip = ip
        self.zc = Zeroconf(ip_version=IPVersion.V4Only)
        self.info = ServiceInfo(
            type_="_app-proto._tcp.local.",
            name="escapepod._app-proto._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=8084,
            server="escapepod.local.",
            properties={"txtv": "0", "lo": "1", "la": "2"},
        )
        self.zc.register_service(self.info)
        log(f"mDNS advertising escapepod.local -> {ip}")
        return ip

    def refresh(self, prefer_target: str = None, wait: float = 30.0) -> str:
        """Re-register with the current LAN IP — after sleep or IP change."""
        try:
            self.stop()
        except Exception:
            pass
        return self.start(prefer_target=prefer_target, wait=wait)

    def check_and_update(self, prefer_target: str = None) -> bool:
        """Re-advertise if the LAN IP has drifted. Returns True if refreshed."""
        current = _detect_local_ip(prefer_target)
        if current and current != self._advertised_ip:
            log(f"mDNS: IP drift {self._advertised_ip} -> {current} — re-advertising")
            self.refresh(prefer_target=prefer_target)
            return True
        return False

    def stop(self):
        if self.zc:
            try:
                if self.info:
                    self.zc.unregister_service(self.info)
                self.zc.close()
            except Exception:
                pass
            self.zc = None
            self._advertised_ip = None


# ── Vector discovery (mDNS) — replaces find-vector.py ─────────────────────────

def discover_vector_ip(timeout: float = 6.0) -> str | None:
    """Browse for Vector's mDNS service; return his current IPv4 or None."""
    try:
        from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        return None

    found: dict[str, str] = {}

    class L(ServiceListener):
        def _grab(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=2000)
            if not info:
                return
            ips = info.parsed_addresses(IPVersion.V4Only) or []
            if ips:
                found[name] = ips[0]

        def add_service(self, zc, t, n):    self._grab(zc, t, n)
        def update_service(self, zc, t, n): self._grab(zc, t, n)
        def remove_service(self, zc, t, n): pass

    zc = Zeroconf(ip_version=IPVersion.V4Only)
    try:
        ServiceBrowser(zc, "_ankivector._tcp.local.", L())
        time.sleep(timeout)
    finally:
        zc.close()
    # Single-robot setups: one result is unambiguous.
    if len(found) == 1:
        return next(iter(found.values()))
    return None


def update_botinfo_ip(new_ip: str) -> bool:
    """Rewrite botSdkInfo.json with new_ip (no BOM — Wire-Pod's parser
    rejects byte-order marks). Returns True if it changed."""
    try:
        data = json.loads(BOTINFO.read_text(encoding="utf-8-sig"))
    except Exception as e:
        log(f"botSdkInfo read failed: {e}")
        return False
    robots = data.get("robots") or []
    if not robots:
        return False
    if robots[0].get("ip_address") == new_ip:
        return False
    old = robots[0].get("ip_address")
    robots[0]["ip_address"] = new_ip
    BOTINFO.write_text(json.dumps(data, separators=(",", ":")),
                       encoding="utf-8", newline="\n")
    log(f"Vector IP drift: {old} -> {new_ip} (botSdkInfo updated)")
    return True


# ── LAN routing — keep Vector's traffic off any VPN/Tailscale overlay ─────────

def ensure_lan_route(vector_ip: str):
    """Pin a direct /32 host route to Vector via the LAN interface. A /32 is
    more specific than any /24 a VPN might advertise, so it always wins.
    Windows-only; on Linux the LAN route is normally already correct."""
    if not IS_WINDOWS or not vector_ip:
        return
    try:
        # Which interface currently reaches Vector?
        chk = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Find-NetRoute -RemoteIPAddress {vector_ip} "
             f"-ErrorAction SilentlyContinue | Select-Object -First 1)"
             f".InterfaceAlias"],
            capture_output=True, text=True, timeout=15)
        iface = (chk.stdout or "").strip()
        if iface and "tailscale" not in iface.lower() and "vpn" not in iface.lower():
            return  # already routing over a sane interface
        # Find the LAN interface (the one holding our LAN IP) and pin a /32.
        ip = local_ip()
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"""
$lan = (Get-NetIPAddress -IPAddress '{ip}' -ErrorAction SilentlyContinue).InterfaceAlias
if ($lan) {{
  Remove-NetRoute -DestinationPrefix '{vector_ip}/32' -Confirm:$false -ErrorAction SilentlyContinue
  New-NetRoute -DestinationPrefix '{vector_ip}/32' -InterfaceAlias $lan -NextHop '0.0.0.0' -RouteMetric 1 -ErrorAction SilentlyContinue | Out-Null
}}"""],
            capture_output=True, text=True, timeout=15)
        log(f"Re-asserted direct LAN /32 route to {vector_ip}")
    except Exception as e:
        log(f"ensure_lan_route error: {e}")


# ── Hosts-file maintenance ────────────────────────────────────────────────────

def update_hosts_file(ip: str) -> None:
    """Keep the escapepod.local entry in the system hosts file current.

    Vector uses mDNS to find this server; the hosts entry serves browsers
    during the initial Wire-Pod setup wizard. Keeping them in sync prevents
    stale entries confusing setup after a LAN IP change. The supervisor runs
    with elevated privileges (RunLevel=Highest), so writes normally succeed."""
    if IS_WINDOWS:
        hosts = Path(r"C:\Windows\System32\drivers\etc\hosts")
    else:
        hosts = Path("/etc/hosts")
    try:
        text = hosts.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log(f"hosts: cannot read ({e})")
        return

    new_line = f"{ip}\tescapepod.local"
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    found = changed = False
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "escapepod.local" in s:
            found = True
            if s.split()[0] != ip:
                out.append(new_line + "\n")
                changed = True
            else:
                out.append(line)
        else:
            out.append(line)
    if not found:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(new_line + "\n")
        changed = True
    if not changed:
        return
    try:
        hosts.write_text("".join(out), encoding="utf-8")
        log(f"hosts: escapepod.local -> {ip}")
        if IS_WINDOWS:
            subprocess.run(["ipconfig", "/flushdns"],
                           capture_output=True, timeout=5)
    except PermissionError:
        log("hosts: permission denied — run the supervisor elevated to auto-update")
    except Exception as e:
        log(f"hosts: write failed: {e}")


# ── Child processes ───────────────────────────────────────────────────────────

class Child:
    def __init__(self, name, argv, cwd, logfile, env=None):
        self.name = name
        self.argv = [str(a) for a in argv]
        self.cwd = str(cwd)
        self.logfile = logfile
        self.env = env
        self.proc = None

    def start(self):
        try:
            fh = open(self.logfile, "ab")
        except OSError:
            fh = subprocess.DEVNULL
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        self.proc = subprocess.Popen(
            self.argv, cwd=self.cwd, stdout=fh, stderr=subprocess.STDOUT,
            env=self.env, creationflags=flags)
        assign_to_job(self.proc.pid)  # so it dies with the supervisor
        log(f"started {self.name} (pid {self.proc.pid})")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if not self.alive():
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        log(f"stopped {self.name}")


# ── The supervisor ────────────────────────────────────────────────────────────

class Supervisor:
    def __init__(self):
        self.shutdown = False
        self.mdns = MDNS()
        self.ollama = None
        self.chipper = None
        self.vectorai = None
        self.vector_was_up = True   # track link transitions

    # -- child definitions --
    def _chipper_env(self) -> dict:
        # Pass chipper's config explicitly rather than relying on machine-wide
        # environment variables — keeps the install self-contained.
        env = dict(os.environ)
        env["STT_SERVICE"] = STT_SERVICE
        env["WHISPER_MODEL"] = WHISPER_MODEL
        env["DISABLE_MDNS"] = "true"   # the supervisor does mDNS itself
        env["DEBUG_LOGGING"] = "true"
        return env

    def start_ollama(self):
        if http_ok("http://127.0.0.1:11434", timeout=2):
            log("Ollama already running")
            self.ollama = None  # not ours to manage
            return
        self.ollama = Child("ollama", ["ollama", "serve"], POD_DIR,
                            POD_DIR / "ollama.log")
        self.ollama.start()
        for _ in range(20):
            if http_ok("http://127.0.0.1:11434", timeout=2):
                return
            time.sleep(1)
        log("WARNING: Ollama did not come up")

    def start_chipper(self):
        self.chipper = Child("chipper", [chipper_binary()], CHIPPER_DIR,
                             CHIPPER_LOG, env=self._chipper_env())
        self.chipper.start()

    def start_vectorai(self):
        py = sys.executable  # we run under the vector-ai venv
        self.vectorai = Child(
            "vector-ai",
            [py, "-u", "-m", "uvicorn", "service:app",
             "--host", "127.0.0.1", "--port", "8000"],
            VECTORAI_DIR, VECTORAI_LOG)
        self.vectorai.start()

    # -- health --
    def chipper_healthy(self) -> bool:
        return (self.chipper and self.chipper.alive()
                and tcp_ok("127.0.0.1", 443, timeout=2))

    def vectorai_healthy(self) -> bool:
        return http_ok("http://127.0.0.1:8000/health", timeout=4)

    def vector_reachable(self) -> bool:
        ip = read_vector_ip()
        return bool(ip) and tcp_ok(ip, 443, timeout=4)

    # -- lifecycle --
    def startup(self):
        # Rotate oversized logs before anything writes to or opens them.
        for logpath in (SUP_LOG, CHIPPER_LOG, VECTORAI_LOG):
            rotate_log(logpath)
        log("=== Vector Pod Supervisor starting ===")
        create_win_job()
        vip = read_vector_ip()
        if vip:
            ensure_lan_route(vip)
        # Pass Vector's known IP as a routing hint so _detect_local_ip() probes
        # the interface that actually talks to Vector — correct even when
        # Tailscale is down and left stale 10.x.x.x routing entries behind.
        lan_ip = self.mdns.start(prefer_target=vip)
        update_hosts_file(lan_ip)
        self.start_ollama()
        self.start_vectorai()
        self.start_chipper()
        log("startup complete")

    def shutdown_all(self):
        log("=== shutting down ===")
        for c in (self.chipper, self.vectorai):
            if c:
                c.stop()
        # Free VRAM: tell Ollama to unload the model (model auto-unload is
        # intentionally kept — we just make it immediate on shutdown).
        try:
            subprocess.run(["ollama", "stop", "gemma3:12b"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        if self.ollama:
            self.ollama.stop()
        self.mdns.stop()
        log("shutdown complete")

    def bounce_chipper(self, why: str):
        log(f"bouncing chipper: {why}")
        if self.chipper:
            self.chipper.stop()
        time.sleep(2)
        self.start_chipper()

    # -- main loop --
    def run(self):
        self.startup()
        last_tick = time.time()
        _mdns_drift_count = 0   # ticks since last mDNS IP-drift check
        while not self.shutdown:
            for _ in range(HEALTH_PERIOD):
                if self.shutdown:
                    break
                time.sleep(1)
            if self.shutdown:
                break

            now = time.time()
            gap = now - last_tick
            last_tick = now

            # Wake-from-sleep: a tick that took far longer than HEALTH_PERIOD
            # means the PC was suspended. Refresh everything network-facing.
            if gap > SLEEP_GAP:
                log(f"wake-from-sleep detected (tick gap {int(gap)}s) — refreshing")
                vip = read_vector_ip()
                # Block up to 60s for the LAN interface to come up; pass
                # Vector's IP so we probe the right interface immediately.
                lan_ip = self.mdns.refresh(prefer_target=vip, wait=60)
                log(f"wake-from-sleep: LAN IP {lan_ip}")
                update_hosts_file(lan_ip)
                if vip:
                    ensure_lan_route(vip)
                self.bounce_chipper("post-sleep refresh")
                self.vector_was_up = True
                _mdns_drift_count = 0
                continue

            # Ollama
            if not http_ok("http://127.0.0.1:11434", timeout=3):
                log("Ollama down — restarting")
                self.start_ollama()

            # vector-ai
            if not self.vectorai_healthy():
                log("vector-ai unhealthy — restarting")
                if self.vectorai:
                    self.vectorai.stop()
                self.start_vectorai()

            # chipper
            if not self.chipper_healthy():
                log("chipper unhealthy — restarting")
                self.bounce_chipper("failed health check")

            # Vector link: detect drop/return. On return, bounce chipper so it
            # rebuilds connections instead of nursing stale ones.
            up = self.vector_reachable()
            if up and not self.vector_was_up:
                log("Vector link recovered — bouncing chipper to reconnect")
                # IP may have changed while he was gone.
                new_ip = discover_vector_ip(timeout=5)
                if new_ip and update_botinfo_ip(new_ip):
                    ensure_lan_route(new_ip)
                self.bounce_chipper("Vector link recovered")
            elif not up and self.vector_was_up:
                log("Vector link lost — will reconnect when it returns")
            self.vector_was_up = up

            # Periodic mDNS/hosts IP-drift check — catches a LAN IP change
            # (DHCP reassignment, interface rebind) without needing a restart.
            # Runs every ~60 s (6 × HEALTH_PERIOD at the default 10 s period).
            _mdns_drift_count += 1
            if _mdns_drift_count >= 6:
                _mdns_drift_count = 0
                vip = read_vector_ip()
                if self.mdns.check_and_update(prefer_target=vip):
                    update_hosts_file(self.mdns._advertised_ip)
                    self.bounce_chipper("LAN IP changed")


def main():
    sup = Supervisor()

    def handle_sig(signum, frame):
        sup.shutdown = True

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        sup.run()
    finally:
        sup.shutdown_all()


if __name__ == "__main__":
    main()
