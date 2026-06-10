#!/usr/bin/env python3
"""Unit test for supervisor.py's SDK-wedge detector.

Feeds the detector synthetic chipper.log lines (taken verbatim from the
issue #8 report) and asserts it bounces only on the real wedge signature:
N robot-RPC hangs within the window while Vector is TCP-reachable, with a
cooldown between bounces. Run directly:  python test_supervisor_wedge.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from supervisor import WedgeDetector, WEDGE_PATTERN, WEDGE_STRIKES, WEDGE_WINDOW, WEDGE_COOLDOWN

# Real lines from the issue #8 chipper.log.
WEDGE_LINES = [
    '[ambient] enable camera feed failed for 0050668d: rpc error: code = DeadlineExceeded desc = context deadline exceeded',
    '[ambient] image capture failed for 0050668d: rpc error: code = DeadlineExceeded desc = context deadline exceeded',
    '[greeting] connect failed for 0050668d: rpc error: code = Unavailable desc = connection error: desc = "transport: Error while dialing: dial tcp 10.0.0.163:443: i/o timeout"',
    '[sensor] event stream failed for 0050668d: rpc error: code = Unavailable desc = connection error: desc = "transport: Error while dialing: dial tcp 10.0.0.163:443: i/o timeout"',
]
# Lines that must NEVER count as strikes.
BENIGN_LINES = [
    # vector-ai HTTP timeout — local service slow, nothing to do with the robot
    '[sensor] vector-ai call failed: Post "http://127.0.0.1:8000/v1/sensor_reaction": context deadline exceeded (Client.Timeout exceeded while awaiting headers)',
    # ordinary stream reset — happens on robot sleep/reboot transitions
    '[sensor] recv error for 0050668d: rpc error: code = Unavailable desc = error reading from server: read tcp 1.2.3.4:63788->5.6.7.8:443: wsarecv: An existing connection was forcibly closed by the remote host.',
    'Bot 0050668d Transcribed text: what is 2+2?',
    '[ambient] starting ambient loop for 0050668d @ 1.2.3.4:443',
]

passed = 0
def check(name, cond):
    global passed
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")
    passed += 1

# 1. Pattern fidelity
for ln in WEDGE_LINES:
    check(f"matches wedge line: {ln[:40]}...", WEDGE_PATTERN.search(ln) is not None)
for ln in BENIGN_LINES:
    check(f"ignores benign line: {ln[:40]}...", WEDGE_PATTERN.search(ln) is None)

# 2. Link-down strikes don't count
d = WedgeDetector()
for i in range(5):
    d.feed(WEDGE_LINES[0], now=100.0 + i, link_up=False)
check("no strikes while link down", len(d.strikes) == 0 and not d.should_bounce(110.0))

# 3. Bounces at the strike threshold, not before
d = WedgeDetector()
t = 1000.0
for i in range(WEDGE_STRIKES - 1):
    d.feed(WEDGE_LINES[i % len(WEDGE_LINES)], now=t + i * 60, link_up=True)
check("below threshold: no bounce", not d.should_bounce(t + 300))
d.feed(WEDGE_LINES[0], now=t + 300, link_up=True)
check("at threshold: bounce", d.should_bounce(t + 300))
check("strikes cleared after bounce", len(d.strikes) == 0)

# 4. Old strikes age out of the window
d = WedgeDetector()
d.feed(WEDGE_LINES[0], now=0.0, link_up=True)
d.feed(WEDGE_LINES[1], now=1.0, link_up=True)
# third strike arrives after the first two have left the window
late = WEDGE_WINDOW + 10.0
d.feed(WEDGE_LINES[2], now=late, link_up=True)
check("stale strikes age out: no bounce", not d.should_bounce(late))

# 5. Cooldown prevents thrashing
d = WedgeDetector()
t = 5000.0
for i in range(WEDGE_STRIKES):
    d.feed(WEDGE_LINES[0], now=t, link_up=True)
check("first wedge: bounce", d.should_bounce(t))
for i in range(WEDGE_STRIKES):
    d.feed(WEDGE_LINES[0], now=t + 60, link_up=True)
check("re-wedge inside cooldown: no bounce", not d.should_bounce(t + 60))
t2 = t + WEDGE_COOLDOWN + 60
for i in range(WEDGE_STRIKES):
    d.feed(WEDGE_LINES[0], now=t2, link_up=True)
check("re-wedge after cooldown: bounce", d.should_bounce(t2))

print(f"\nAll {passed} checks passed.")
