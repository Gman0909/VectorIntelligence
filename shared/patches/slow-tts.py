#!/usr/bin/env python3
"""Tune Vector's TTS cadence.

Wire-Pod ships with DurationScalar=0.95 (~5% faster than natural). We set it
to 1.0 (natural pace) — a balance between intelligibility and not sounding
sluggish during longer responses. Higher = slower; 1.1 is noticeably slow,
0.95 is the rushed default. Tune by changing the value below or passing it
on the command line.

Usage:
    slow-tts.py <path-to-kgsim_cmds.go> [scalar]

Idempotent: detects current value and only writes if different.
"""
import re
import sys
from pathlib import Path

DEFAULT_SCALAR = "1.0"


def patch_file(path: Path, scalar: str) -> bool:
    src = path.read_text()
    pattern = re.compile(r'DurationScalar:\s*[0-9.]+,')
    matches = pattern.findall(src)
    if not matches:
        print(f"[slow-tts] DurationScalar not found in {path}", file=sys.stderr)
        sys.exit(1)
    target = f'DurationScalar: {scalar},'
    if all(m == target for m in matches):
        print(f"[slow-tts] {path.name} already set to {scalar}, skipping.")
        return False
    src = pattern.sub(target, src)
    path.write_text(src)
    print(f"[slow-tts] {path.name} DurationScalar set to {scalar}.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path>", file=sys.stderr)
        sys.exit(2)
    target_path = Path(sys.argv[1])
    scalar = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SCALAR
    patch_file(target_path, scalar)
