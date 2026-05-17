#!/usr/bin/env python3
"""Add a concurrent face probe so Vector knows the speaker before he replies.

Faces normally only reach the server through the per-response interrupt
stream, so the face used to scope query N is whoever was seen during
response N-1 — a one-turn lag (a stranger is greeted on their *second*
utterance, a speaker hand-off lands a turn late).

This patch adds ObserveFaceBriefly(), launched as a goroutine at the start
of a voice request — it runs concurrently with speech-to-text, so by the
time the LLM request fires vector-ai already knows the current speaker.
Zero added latency: it overlaps the user speaking.

Creates chipper/pkg/wirepod/ttr/face_probe.go and patches intent_graph.go.
Idempotent.
"""
import sys
from pathlib import Path

FACE_PROBE_FILENAME = "face_probe.go"

FACE_PROBE_GO = '''package wirepod_ttr

import (
\t"context"
\t"fmt"
\t"strings"
\t"time"

\t"github.com/fforchino/vector-go-sdk/pkg/vector"
\t"github.com/fforchino/vector-go-sdk/pkg/vectorpb"
\t"github.com/kercre123/wire-pod/chipper/pkg/vars"
)

// ObserveFaceBriefly opens a short-lived robot_observed_face stream and
// reports who Vector is looking at to vector-ai (via notifyFaceSeen). It is
// launched as a goroutine at the START of a voice request, so it runs
// concurrently with speech-to-text — by the time the LLM request fires,
// vector-ai already knows the current speaker, with no one-turn lag and no
// added latency. Self-terminating: closes after the timeout or stream end.
func ObserveFaceBriefly(esn string) {
\tvar guid, target string
\tfor _, bot := range vars.BotInfo.Robots {
\t\tif strings.EqualFold(strings.TrimSpace(bot.Esn), strings.TrimSpace(esn)) {
\t\t\tguid = bot.GUID
\t\t\ttarget = bot.IPAddress + ":443"
\t\t\tbreak
\t\t}
\t}
\tif target == "" {
\t\treturn
\t}
\trobot, err := vector.New(vector.WithSerialNo(esn), vector.WithToken(guid), vector.WithTarget(target))
\tif err != nil {
\t\tfmt.Printf("[face-probe] connect failed for %s: %v\\n", esn, err)
\t\treturn
\t}
\t// Release the gRPC connection when the probe ends — otherwise it leaks.
\tdefer robot.Close()
\tctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
\tdefer cancel()
\tstrm, err := robot.Conn.EventStream(
\t\tctx,
\t\t&vectorpb.EventRequest{
\t\t\tListType: &vectorpb.EventRequest_WhiteList{
\t\t\t\tWhiteList: &vectorpb.FilterList{List: []string{"robot_observed_face"}},
\t\t\t},
\t\t},
\t)
\tif err != nil {
\t\tfmt.Printf("[face-probe] event stream failed for %s: %v\\n", esn, err)
\t\treturn
\t}
\tfor {
\t\tresp, err := strm.Recv()
\t\tif err != nil {
\t\t\treturn // context timeout or stream closed — probe done
\t\t}
\t\tif rof := resp.Event.GetRobotObservedFace(); rof != nil {
\t\t\tnotifyFaceSeen(rof.GetFaceId(), rof.GetName())
\t\t}
\t}
}
'''

SENTINEL_INTENT = "ObserveFaceBriefly"

ANCHOR = (
    "\tspeechReq := sr.ReqToSpeechRequest(req)\n"
    "\tvar transcribedText string\n"
)
REPLACEMENT = (
    "\tspeechReq := sr.ReqToSpeechRequest(req)\n"
    "\t// Observe the speaker's face concurrently with speech-to-text so the\n"
    "\t// current person is known before the LLM request fires (no one-turn lag).\n"
    "\tgo ttr.ObserveFaceBriefly(speechReq.Device)\n"
    "\tvar transcribedText string\n"
)


def write_face_probe(ttr_dir: Path) -> bool:
    target = ttr_dir / FACE_PROBE_FILENAME
    if target.exists() and target.read_text(encoding="utf-8") == FACE_PROBE_GO:
        print(f"[face-probe] {FACE_PROBE_FILENAME} already in place.")
        return False
    target.write_text(FACE_PROBE_GO, encoding="utf-8", newline="\n")
    print(f"[face-probe] wrote {target}")
    return True


def patch_intent_graph(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if SENTINEL_INTENT in src:
        print(f"[face-probe] {path.name} already patched.")
        return False
    if ANCHOR not in src:
        print(f"[face-probe] anchor not found in {path}", file=sys.stderr)
        sys.exit(1)
    src = src.replace(ANCHOR, REPLACEMENT, 1)
    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[face-probe] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-wire-pod-dir>", file=sys.stderr)
        sys.exit(2)
    wirepod = Path(sys.argv[1])
    ttr_dir = wirepod / "chipper" / "pkg" / "wirepod" / "ttr"
    intent_graph = wirepod / "chipper" / "pkg" / "wirepod" / "preqs" / "intent_graph.go"
    if not ttr_dir.exists() or not intent_graph.exists():
        print(f"[face-probe] target paths not found under {wirepod}", file=sys.stderr)
        sys.exit(1)
    write_face_probe(ttr_dir)
    patch_intent_graph(intent_graph)
