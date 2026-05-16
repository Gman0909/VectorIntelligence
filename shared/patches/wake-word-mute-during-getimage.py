#!/usr/bin/env python3
"""Mute Wire-Pod's wake-word interrupt while a getImage is in progress.

The photo shutter animation ("1...2...3...click") plays through Vector's
own speaker. His on-device wake-word detector hears it and misfires, which
aborts the LLM response before Vector ever speaks it. This patch:

1. Adds a package-level `WakeWordMutedUntil` time variable in the ttr package
2. DoGetImage bumps it forward by ~6 seconds whenever it runs (covers the
   shutter animation + a buffer for lingering sound)
3. The wake-word interrupt loop skips events when time.Now() is before
   WakeWordMutedUntil

Idempotent.
"""
import re
import sys
from pathlib import Path

CMDS_RELATIVE     = Path("chipper/pkg/wirepod/ttr/kgsim_cmds.go")
INTERRUPT_RELATIVE= Path("chipper/pkg/wirepod/ttr/kgsim_interrupt.go")


def patch_cmds(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if "WakeWordMutedUntil" in src:
        print(f"[mute-on-getimage] {path.name} already patched.")
        return False

    # Add the package-level variable after the imports block.
    # NOTE: stick to ASCII — Python's default file encoding on Windows is
    # cp1252, and any non-ASCII char gets mangled into invalid UTF-8 when
    # Go reads the file.
    var_block = (
        "\n// WakeWordMutedUntil is checked by the wake-word interrupt loop in\n"
        "// kgsim_interrupt.go. While time.Now() is before this value, wake-word\n"
        "// events are ignored - used to prevent Vector's own shutter sound from\n"
        "// self-interrupting the response during getImage.\n"
        "var WakeWordMutedUntil time.Time\n"
    )
    # Insert AFTER the imports closing ')' but BEFORE the `const (` block.
    # The capture group splits on the ')' so we can put the var block in
    # the right place.
    new_src, n = re.subn(
        r"(\)\n)(\nconst \(\n\t// arg: text to say)",
        r"\g<1>" + var_block + r"\g<2>",
        src, count=1,
    )
    if n != 1:
        print(f"[mute-on-getimage] anchor not found in {path}", file=sys.stderr)
        sys.exit(1)

    # Set the mute window when DoGetImage starts.
    mute_set = (
        '\tWakeWordMutedUntil = time.Now().Add(6 * time.Second)\n'
        '\tlogger.Println("Muting wake-word interrupts for ~6s (getImage)")\n'
    )
    new_src, n = re.subn(
        r"func DoGetImage\(msgs \[\]openai\.ChatCompletionMessage, param string, robot \*vector\.Vector, stopStop chan bool\) \{\n",
        r"\g<0>" + mute_set,
        new_src, count=1,
    )
    if n != 1:
        print(f"[mute-on-getimage] DoGetImage anchor not found", file=sys.stderr)
        sys.exit(1)

    path.write_text(new_src, encoding="utf-8", newline="")
    print(f"[mute-on-getimage] {path.name} patched.")
    return True


def patch_interrupt(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if "WakeWordMutedUntil" in src:
        print(f"[mute-on-getimage] {path.name} already patched.")
        return False

    OLD = """\t\t\tcase *vectorpb.Event_WakeWord:
\t\t\t\tif time.Since(startTime) < wakeWordGrace {
\t\t\t\t\tlogger.Println(\"Ignoring wake-word during grace period\")
\t\t\t\t\tcontinue
\t\t\t\t}
\t\t\t\tlogger.Println(\"Interrupting LLM response (source: wake word)\")
\t\t\t\tstopResponse = true"""

    NEW = """\t\t\tcase *vectorpb.Event_WakeWord:
\t\t\t\tif time.Since(startTime) < wakeWordGrace {
\t\t\t\t\tlogger.Println(\"Ignoring wake-word during grace period\")
\t\t\t\t\tcontinue
\t\t\t\t}
\t\t\t\tif time.Now().Before(WakeWordMutedUntil) {
\t\t\t\t\tlogger.Println(\"Ignoring wake-word during getImage mute window\")
\t\t\t\t\tcontinue
\t\t\t\t}
\t\t\t\tlogger.Println(\"Interrupting LLM response (source: wake word)\")
\t\t\t\tstopResponse = true"""

    if OLD not in src:
        print(f"[mute-on-getimage] anchor not found in {path}", file=sys.stderr)
        sys.exit(1)
    src = src.replace(OLD, NEW)
    path.write_text(src, encoding="utf-8", newline="")
    print(f"[mute-on-getimage] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path>", file=sys.stderr)
        sys.exit(2)
    wirepod_root = Path(sys.argv[1])
    patch_cmds(wirepod_root / CMDS_RELATIVE)
    patch_interrupt(wirepod_root / INTERRUPT_RELATIVE)
