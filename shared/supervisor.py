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

EXE = ".exe" if IS_WINDOWS else ""


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [supervisor] {msg}"
    print(line, flush=True)
    try:
        with open(SUP_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── Small helpers ─────────────────────────────────────────────────────────────

_last_good_ip = None  # remembered so a post-wake detection failure can't
                      # silently downgrade mDNS to loopback


def _detect_local_ip():
    """Best-effort LAN IP via the UDP-connect trick. Returns None when no
    real (non-loopback) address is available — typically because the network
    interface hasn't come up yet, e.g. in the seconds after wake-from-sleep."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    if not ip or ip == "0.0.0.0" or ip.startswith("127."):
        return None
    return ip


def local_ip(wait: float = 0.0) -> str:
    """This machine's LAN IP (the interface that routes to the LAN).

    Polls for up to `wait` seconds for a real address — the network is often
    not ready for a second or two after wake-from-sleep, and advertising
    loopback over mDNS points Vector at itself (the classic "responds once
    then dead" failure). Falls back to the last known-good IP, and only as a
    true last resort to loopback."""
    global _last_good_ip
    deadline = time.monotonic() + wait
    while True:
        ip = _detect_local_ip()
        if ip:
            _last_good_ip = ip
            return ip
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    if _last_good_ip:
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

    def start(self):
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
        # wait: tolerate a network interface that is still coming up (post-wake).
        ip = local_ip(wait=30)
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

    def refresh(self):
        """Re-register after a sleep — the multicast socket goes stale."""
        try:
            self.stop()
        except Exception:
            pass
        self.start()

    def stop(self):
        if self.zc:
            try:
                if self.info:
                    self.zc.unregister_service(self.info)
                self.zc.close()
            except Exception:
                pass
            self.zc = None


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
        log("=== Vector Pod Supervisor starting ===")
        create_win_job()
        vip = read_vector_ip()
        if vip:
            ensure_lan_route(vip)
        self.mdns.start()
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
            subprocess.run(["ollama", "stop", "gemma4:e4b"],
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
                # The network interface is usually not up yet right after
                # wake. Block until a real LAN IP is available (up to 60s)
                # before re-advertising — otherwise mDNS publishes loopback
                # and Vector tries to reach the server on itself.
                log(f"wake-from-sleep: waiting for network, LAN IP {local_ip(wait=60)}")
                self.mdns.refresh()
                vip = read_vector_ip()
                if vip:
                    ensure_lan_route(vip)
                self.bounce_chipper("post-sleep refresh")
                self.vector_was_up = True
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
