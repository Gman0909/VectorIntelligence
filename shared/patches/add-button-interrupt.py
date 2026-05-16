#!/usr/bin/env python3
"""Make Vector's back button interrupt his speech.

Wire-Pod's interrupt loop (kgsim_interrupt.go) already stops a response on
a wake-word event or a touch-sensor spike, but ignores the physical back
button — which registers as the IS_BUTTON_PRESSED status bit in
robot_state, not as a wake-word event. This patch adds a check for that
bit so pressing the button stops him talking immediately.

No grace period for the button: a deliberate physical press can't be a
false trigger from Vector's own opening words (unlike the wake word), so
it interrupts at any point in the response.

Idempotent. Modifies chipper/pkg/wirepod/ttr/kgsim_interrupt.go.
"""
import sys
from pathlib import Path

SENTINEL = "source: back button"

ANCHOR = (
    "\t\t\tcase *vectorpb.Event_RobotState:\n"
    "\t\t\t\tif resp.Event.GetRobotState().TouchData.GetRawTouchValue() > origTouchValue+50 {\n"
    "\t\t\t\t\tvalsAboveValue++\n"
    "\t\t\t\t} else {\n"
    "\t\t\t\t\tvalsAboveValue = 0\n"
    "\t\t\t\t}\n"
)

REPLACEMENT = (
    "\t\t\tcase *vectorpb.Event_RobotState:\n"
    "\t\t\t\trsEvt := resp.Event.GetRobotState()\n"
    "\t\t\t\tif rsEvt.TouchData.GetRawTouchValue() > origTouchValue+50 {\n"
    "\t\t\t\t\tvalsAboveValue++\n"
    "\t\t\t\t} else {\n"
    "\t\t\t\t\tvalsAboveValue = 0\n"
    "\t\t\t\t}\n"
    "\t\t\t\t// Physical back button — interrupt immediately, no grace period.\n"
    "\t\t\t\tif rsEvt.Status&uint32(vectorpb.RobotStatus_ROBOT_STATUS_IS_BUTTON_PRESSED) != 0 {\n"
    "\t\t\t\t\tlogger.Println(\"Interrupting LLM response (source: back button)\")\n"
    "\t\t\t\t\tstopResponse = true\n"
    "\t\t\t\t}\n"
)


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if SENTINEL in src:
        print(f"[button-interrupt] {path.name} already patched.")
        return False
    if ANCHOR not in src:
        print(f"[button-interrupt] anchor not found in {path}", file=sys.stderr)
        sys.exit(1)
    src = src.replace(ANCHOR, REPLACEMENT, 1)
    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[button-interrupt] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-kgsim_interrupt.go>", file=sys.stderr)
        sys.exit(2)
    patch(Path(sys.argv[1]))
