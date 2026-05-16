#!/usr/bin/env python3
"""Add on-demand face detection to Wire-Pod's response interrupter.

Vector should know who he's talking to so memory can be scoped per face,
but subscribing to robot_observed_face 24/7 is a firehose — Vector emits a
face event on every frame, which overloads his firmware and degrades his
whole network stack over time.

This patch adds robot_observed_face to the interrupt loop's event whitelist
*only*. That stream opens per-response and closes when the response ends,
so face detection runs solely for the lifetime of a voice interaction —
never a continuous background stream.

Observed faces are reported to vector-ai via notifyFaceSeen (defined in
sensor_reactions.go by add-sensor-reactions.py), which rate-limits the calls.

Run after the other kgsim_interrupt.go patches (wake-word-grace-period,
add-button-interrupt, wake-word-mute-during-getimage) — its anchors are
chosen not to collide with theirs, but this keeps the ordering obvious.

Idempotent. Modifies chipper/pkg/wirepod/ttr/kgsim_interrupt.go.
"""
import sys
from pathlib import Path

SENTINEL = "Event_RobotObservedFace"

# 1. Add robot_observed_face to the interrupt stream's event whitelist.
ANCHOR_WHITELIST = '\t\t\t\t\tList: []string{"robot_state", "wake_word"},\n'
REPLACE_WHITELIST = (
    "\t\t\t\t\t// robot_observed_face is included here — and ONLY here —\n"
    "\t\t\t\t\t// so face detection runs only for the lifetime of a voice\n"
    "\t\t\t\t\t// interaction (this stream opens per-response and closes\n"
    "\t\t\t\t\t// when it ends). Never a 24/7 firehose.\n"
    '\t\t\t\t\tList: []string{"robot_state", "wake_word", "robot_observed_face"},\n'
)

# 2. Handle the face event in the interrupt switch. Anchored on the second
#    switch's `default:` + the valsAboveValue check that uniquely follows it
#    (the first switch's default is followed by `if origValueGotten`).
ANCHOR_CASE = (
    "\t\t\tdefault:\n"
    "\t\t\t}\n"
    "\t\t\tif valsAboveValue > valsAboveValueMax {\n"
)
REPLACE_CASE = (
    "\t\t\tcase *vectorpb.Event_RobotObservedFace:\n"
    "\t\t\t\t// On-demand face detection: report who Vector sees so the\n"
    "\t\t\t\t// next turn's memory scoping knows the speaker. Runs only\n"
    "\t\t\t\t// during this interaction; notifyFaceSeen is rate-limited.\n"
    "\t\t\t\tif rof := resp.Event.GetRobotObservedFace(); rof != nil {\n"
    "\t\t\t\t\tnotifyFaceSeen(rof.GetFaceId(), rof.GetName())\n"
    "\t\t\t\t}\n"
    "\t\t\tdefault:\n"
    "\t\t\t}\n"
    "\t\t\tif valsAboveValue > valsAboveValueMax {\n"
)


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if SENTINEL in src:
        print(f"[ondemand-face] {path.name} already patched.")
        return False
    for anchor in (ANCHOR_WHITELIST, ANCHOR_CASE):
        if anchor not in src:
            print(f"[ondemand-face] anchor not found in {path}", file=sys.stderr)
            sys.exit(1)
    src = src.replace(ANCHOR_WHITELIST, REPLACE_WHITELIST, 1)
    src = src.replace(ANCHOR_CASE, REPLACE_CASE, 1)
    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[ondemand-face] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-kgsim_interrupt.go>", file=sys.stderr)
        sys.exit(2)
    patch(Path(sys.argv[1]))
