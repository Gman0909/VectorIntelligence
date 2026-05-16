#!/usr/bin/env python3
"""Fix the gRPC connection leak in Wire-Pod's LLM response path.

The fforchino vector-go-sdk opens a gRPC connection to the robot on every
vector.New() but never closes it. Wire-Pod's kgsim.go calls vector.New()
once per voice query (and once per speak-goroutine), so each query leaks a
connection. The robot's on-board SDK has a small connection budget; once it
fills up Vector stops responding and shows the wifi-exclamation icon — the
classic "drops after a question or two" failure.

This patch adds `defer robot.Close()` at the two leak sites in kgsim.go and
gives the opening BatteryState probe a 5s timeout, so a wedged robot fails
fast instead of hanging on context.Background() forever.

Requires the SDK Close() method — see add-sdk-close.py, which must run first.

Idempotent. Modifies chipper/pkg/wirepod/ttr/kgsim.go.
"""
import sys
from pathlib import Path

SENTINEL = "defer robot.Close()"

# Leak site 1: the per-request robot created in the `if matched` block, plus
# the unbounded BatteryState probe right after it.
ANCHOR_BAT = (
    "\t\trobot, err = vector.New(vector.WithSerialNo(esn), vector.WithToken(guid), vector.WithTarget(target))\n"
    "\t\tif err != nil {\n"
    "\t\t\treturn err.Error(), err\n"
    "\t\t}\n"
    "\t}\n"
    "\t_, err := robot.Conn.BatteryState(context.Background(), &vectorpb.BatteryStateRequest{})\n"
)
REPLACE_BAT = (
    "\t\trobot, err = vector.New(vector.WithSerialNo(esn), vector.WithToken(guid), vector.WithTarget(target))\n"
    "\t\tif err != nil {\n"
    "\t\t\treturn err.Error(), err\n"
    "\t\t}\n"
    "\t\t// Release the gRPC connection when this request finishes — otherwise\n"
    "\t\t// every voice query leaks a connection and the robot's SDK wedges.\n"
    "\t\tdefer robot.Close()\n"
    "\t}\n"
    "\t// 5s timeout: if the robot's SDK is unresponsive this fails fast instead\n"
    "\t// of hanging forever on context.Background().\n"
    "\tbatCtx, batCancel := context.WithTimeout(context.Background(), 5*time.Second)\n"
    "\t_, err := robot.Conn.BatteryState(batCtx, &vectorpb.BatteryStateRequest{})\n"
    "\tbatCancel()\n"
)

# Leak site 2: the speak-goroutine that acquires behavior control.
ANCHOR_GO = (
    "\tgo func() {\n"
    "\t\tstart := make(chan bool)\n"
    "\t\tstop := make(chan bool)\n"
)
REPLACE_GO = (
    "\tgo func() {\n"
    "\t\t// Release the robot connection when this speak-goroutine finishes.\n"
    "\t\tdefer robot.Close()\n"
    "\t\tstart := make(chan bool)\n"
    "\t\tstop := make(chan bool)\n"
)


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if SENTINEL in src:
        print(f"[connection-leak] {path.name} already patched.")
        return False
    for anchor in (ANCHOR_BAT, ANCHOR_GO):
        if anchor not in src:
            print(f"[connection-leak] anchor not found in {path}", file=sys.stderr)
            sys.exit(1)
    src = src.replace(ANCHOR_BAT, REPLACE_BAT, 1)
    src = src.replace(ANCHOR_GO, REPLACE_GO, 1)
    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[connection-leak] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-kgsim.go>", file=sys.stderr)
        sys.exit(2)
    patch(Path(sys.argv[1]))
