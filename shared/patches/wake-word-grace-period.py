#!/usr/bin/env python3
"""Add a 5-second wake-word grace period to Wire-Pod's response interrupter.

Without this, false-positive wake words during the silent gap between
"end of user speech" and "start of LLM response" abort the reply before
Vector ever speaks it. Touch interrupts (petting Vector to stop him) are
preserved unchanged.

Idempotent: detects whether the patch is already applied and skips if so.
"""
import sys
from pathlib import Path

MARKER = "Wake-word grace period"

OLD_BLOCK = """\tif origValueGotten {
\t\tfor {
\t\t\tvar resp *vectorpb.EventResponse
\t\t\tresp, err = strm.Recv()
\t\t\tif err != nil {
\t\t\t\tlogger.Println(\"Event stream error: \" + err.Error())
\t\t\t\treturn false
\t\t\t}
\t\t\tswitch resp.Event.EventType.(type) {
\t\t\tcase *vectorpb.Event_RobotState:
\t\t\t\tif resp.Event.GetRobotState().TouchData.GetRawTouchValue() > origTouchValue+50 {
\t\t\t\t\tvalsAboveValue++
\t\t\t\t} else {
\t\t\t\t\tvalsAboveValue = 0
\t\t\t\t}
\t\t\tcase *vectorpb.Event_WakeWord:
\t\t\t\tlogger.Println(\"Interrupting LLM response (source: wake word)\")
\t\t\t\tstopResponse = true
\t\t\tdefault:
\t\t\t}"""

NEW_BLOCK = """\tif origValueGotten {
\t\t// Wake-word grace period: ignore wake-word events for the first few
\t\t// seconds so false positives during the LLM-thinking silence (or
\t\t// Vector's own motor noise) don't kill the response before it starts.
\t\tstartTime := time.Now()
\t\tconst wakeWordGrace = 5 * time.Second
\t\tfor {
\t\t\tvar resp *vectorpb.EventResponse
\t\t\tresp, err = strm.Recv()
\t\t\tif err != nil {
\t\t\t\tlogger.Println(\"Event stream error: \" + err.Error())
\t\t\t\treturn false
\t\t\t}
\t\t\tswitch resp.Event.EventType.(type) {
\t\t\tcase *vectorpb.Event_RobotState:
\t\t\t\tif resp.Event.GetRobotState().TouchData.GetRawTouchValue() > origTouchValue+50 {
\t\t\t\t\tvalsAboveValue++
\t\t\t\t} else {
\t\t\t\t\tvalsAboveValue = 0
\t\t\t\t}
\t\t\tcase *vectorpb.Event_WakeWord:
\t\t\t\tif time.Since(startTime) < wakeWordGrace {
\t\t\t\t\tlogger.Println(\"Ignoring wake-word during grace period\")
\t\t\t\t\tcontinue
\t\t\t\t}
\t\t\t\tlogger.Println(\"Interrupting LLM response (source: wake word)\")
\t\t\t\tstopResponse = true
\t\t\tdefault:
\t\t\t}"""


def patch_file(path: Path) -> bool:
    src = path.read_text()
    if "wakeWordGrace" in src:
        print(f"[wake-word-grace] {path.name} already patched, skipping.")
        return False
    if OLD_BLOCK not in src:
        print(f"[wake-word-grace] anchor not found in {path}", file=sys.stderr)
        sys.exit(1)
    src = src.replace(OLD_BLOCK, NEW_BLOCK)
    path.write_text(src)
    print(f"[wake-word-grace] {path.name} patched: wake-word grace period added.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path>", file=sys.stderr)
        sys.exit(2)
    target = Path(sys.argv[1])
    patch_file(target)
