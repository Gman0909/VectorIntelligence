#!/usr/bin/env python3
"""Fix the BehaviorControl stream leak in Wire-Pod's sayText helper.

Upstream sayText (chipper/pkg/wirepod/ttr/bcontrol.go) opens a
BehaviorControl stream with context.Background() and never cancels it.
Sending ControlRelease ends the control reservation but NOT the stream —
the robot's vic-gateway keeps it open until the underlying gRPC connection
closes. Callers on short-lived connections get away with that; the sensor
reaction loop (add-sensor-reactions.py) calls sayText on a connection that
stays up for days, so every pickup/putdown/pet reaction permanently leaks
one server-side stream on the robot. Enough of them and vic-gateway stops
serving new requests: TCP still connects, new RPCs hang to deadline, Vector
shows the wifi-exclamation icon (issue #8's "works for a few commands then
dies" — and its background twin, wedging with no interaction at all).

Two further upstream bugs in the same function go with it:
  - the inner goroutine busy-waits on `select { default: continue }`,
    burning a CPU core for the whole utterance;
  - the outer goroutine ends with `for range start` on a channel nobody
    closes, so it blocks forever — one leaked goroutine per call, and on
    the no-grant path the leak happens before the robot even speaks.

The replacement does the acquire-speak-release cycle synchronously in one
goroutine under a 30s context that is cancelled on exit — the cancel is
what actually tears the stream down robot-side. No channels, no spin.

Idempotent. Modifies chipper/pkg/wirepod/ttr/bcontrol.go.
"""
import sys
from pathlib import Path

SENTINEL = "acquire-speak-release"

ANCHOR_IMPORTS = (
    "import (\n"
    "\t\"context\"\n"
    "\t\"log\"\n"
)
REPLACE_IMPORTS = (
    "import (\n"
    "\t\"context\"\n"
    "\t\"log\"\n"
    "\t\"time\"\n"
)

ANCHOR_FUNC = (
    "func sayText(robot *vector.Vector, text string) {\n"
    "\tcontrolRequest := &vectorpb.BehaviorControlRequest{\n"
    "\t\tRequestType: &vectorpb.BehaviorControlRequest_ControlRequest{\n"
    "\t\t\tControlRequest: &vectorpb.ControlRequest{\n"
    "\t\t\t\tPriority: vectorpb.ControlRequest_OVERRIDE_BEHAVIORS,\n"
    "\t\t\t},\n"
    "\t\t},\n"
    "\t}\n"
    "\tgo func() {\n"
    "\t\tstart := make(chan bool)\n"
    "\t\tstop := make(chan bool)\n"
    "\t\tgo func() {\n"
    "\t\t\t// * begin - modified from official vector-go-sdk\n"
    "\t\t\tr, err := robot.Conn.BehaviorControl(\n"
    "\t\t\t\tcontext.Background(),\n"
    "\t\t\t)\n"
    "\t\t\tif err != nil {\n"
    "\t\t\t\tlog.Println(err)\n"
    "\t\t\t\treturn\n"
    "\t\t\t}\n"
    "\n"
    "\t\t\tif err := r.Send(controlRequest); err != nil {\n"
    "\t\t\t\tlog.Println(err)\n"
    "\t\t\t\treturn\n"
    "\t\t\t}\n"
    "\n"
    "\t\t\tfor {\n"
    "\t\t\t\tctrlresp, err := r.Recv()\n"
    "\t\t\t\tif err != nil {\n"
    "\t\t\t\t\tlog.Println(err)\n"
    "\t\t\t\t\treturn\n"
    "\t\t\t\t}\n"
    "\t\t\t\tif ctrlresp.GetControlGrantedResponse() != nil {\n"
    "\t\t\t\t\tstart <- true\n"
    "\t\t\t\t\tbreak\n"
    "\t\t\t\t}\n"
    "\t\t\t}\n"
    "\n"
    "\t\t\tfor {\n"
    "\t\t\t\tselect {\n"
    "\t\t\t\tcase <-stop:\n"
    "\t\t\t\t\tif err := r.Send(\n"
    "\t\t\t\t\t\t&vectorpb.BehaviorControlRequest{\n"
    "\t\t\t\t\t\t\tRequestType: &vectorpb.BehaviorControlRequest_ControlRelease{\n"
    "\t\t\t\t\t\t\t\tControlRelease: &vectorpb.ControlRelease{},\n"
    "\t\t\t\t\t\t\t},\n"
    "\t\t\t\t\t\t},\n"
    "\t\t\t\t\t); err != nil {\n"
    "\t\t\t\t\t\tlog.Println(err)\n"
    "\t\t\t\t\t\treturn\n"
    "\t\t\t\t\t}\n"
    "\t\t\t\t\treturn\n"
    "\t\t\t\tdefault:\n"
    "\t\t\t\t\tcontinue\n"
    "\t\t\t\t}\n"
    "\t\t\t}\n"
    "\t\t\t// * end - modified from official vector-go-sdk\n"
    "\t\t}()\n"
    "\t\tfor range start {\n"
    "\t\t\trobot.Conn.SayText(\n"
    "\t\t\t\tcontext.Background(),\n"
    "\t\t\t\t&vectorpb.SayTextRequest{\n"
    "\t\t\t\t\tText:           text,\n"
    "\t\t\t\t\tUseVectorVoice: true,\n"
    "\t\t\t\t\tDurationScalar: 1.0,\n"
    "\t\t\t\t},\n"
    "\t\t\t)\n"
    "\t\t\tstop <- true\n"
    "\t\t}\n"
    "\t}()\n"
    "}\n"
)

REPLACE_FUNC = (
    "func sayText(robot *vector.Vector, text string) {\n"
    "\tcontrolRequest := &vectorpb.BehaviorControlRequest{\n"
    "\t\tRequestType: &vectorpb.BehaviorControlRequest_ControlRequest{\n"
    "\t\t\tControlRequest: &vectorpb.ControlRequest{\n"
    "\t\t\t\tPriority: vectorpb.ControlRequest_OVERRIDE_BEHAVIORS,\n"
    "\t\t\t},\n"
    "\t\t},\n"
    "\t}\n"
    "\tgo func() {\n"
    "\t\t// One bounded, cancelled context for the whole acquire-speak-release\n"
    "\t\t// cycle. The cancel is what actually ends the BehaviorControl stream\n"
    "\t\t// on the robot — ControlRelease alone leaves it open, and on a\n"
    "\t\t// long-lived connection (the sensor-reaction loop) those leaked\n"
    "\t\t// streams accumulate until vic-gateway stops serving new requests:\n"
    "\t\t// TCP still up, new RPCs hang, Vector shows the wifi icon.\n"
    "\t\tctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)\n"
    "\t\tdefer cancel()\n"
    "\t\tr, err := robot.Conn.BehaviorControl(ctx)\n"
    "\t\tif err != nil {\n"
    "\t\t\tlog.Println(err)\n"
    "\t\t\treturn\n"
    "\t\t}\n"
    "\t\tif err := r.Send(controlRequest); err != nil {\n"
    "\t\t\tlog.Println(err)\n"
    "\t\t\treturn\n"
    "\t\t}\n"
    "\t\tfor {\n"
    "\t\t\tctrlresp, err := r.Recv()\n"
    "\t\t\tif err != nil {\n"
    "\t\t\t\tlog.Println(err)\n"
    "\t\t\t\treturn\n"
    "\t\t\t}\n"
    "\t\t\tif ctrlresp.GetControlGrantedResponse() != nil {\n"
    "\t\t\t\tbreak\n"
    "\t\t\t}\n"
    "\t\t}\n"
    "\t\t// SayText returns when the utterance finishes (or ctx expires).\n"
    "\t\trobot.Conn.SayText(\n"
    "\t\t\tctx,\n"
    "\t\t\t&vectorpb.SayTextRequest{\n"
    "\t\t\t\tText:           text,\n"
    "\t\t\t\tUseVectorVoice: true,\n"
    "\t\t\t\tDurationScalar: 1.0,\n"
    "\t\t\t},\n"
    "\t\t)\n"
    "\t\tr.Send(&vectorpb.BehaviorControlRequest{\n"
    "\t\t\tRequestType: &vectorpb.BehaviorControlRequest_ControlRelease{\n"
    "\t\t\t\tControlRelease: &vectorpb.ControlRelease{},\n"
    "\t\t\t},\n"
    "\t\t})\n"
    "\t}()\n"
    "}\n"
)


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if SENTINEL in src:
        print(f"[saytext-leak] {path.name} already patched.")
        return False
    for anchor in (ANCHOR_IMPORTS, ANCHOR_FUNC):
        if anchor not in src:
            print(f"[saytext-leak] anchor not found in {path}", file=sys.stderr)
            sys.exit(1)
    src = src.replace(ANCHOR_IMPORTS, REPLACE_IMPORTS, 1)
    src = src.replace(ANCHOR_FUNC, REPLACE_FUNC, 1)
    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[saytext-leak] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-bcontrol.go>", file=sys.stderr)
        sys.exit(2)
    patch(Path(sys.argv[1]))
