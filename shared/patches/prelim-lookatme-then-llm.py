#!/usr/bin/env python3
"""Turn Vector to face the speaker before the LLM replies.

On every LLM-bound voice query (not just vision ones), dispatch
intent_imperative_lookatme via IntentPass BEFORE calling the LLM — Vector's
firmware uses his still-fresh sound-direction cache to rapid-turn toward
whoever just spoke. Skipped when he's on the charger, so he never drives
off his pod (vision queries get a brief pause so he faces the user before
describing the scene).

Dispatching the intent closes the chipper voice stream (IsFinal=true), but
StreamingKGSim's response goes through the SDK (robot.Conn.SayText), so the
LLM answer still reaches the user. StreamingKGSim's own
IntentPass(intent_greeting_hello) then fails silently on the closed
stream — harmless.

Requires ttr.IsOnCharger() — see add-sensor-reactions.py.

Modifies preqs/intent_graph.go. Idempotent.
"""
import re
import sys
from pathlib import Path

SENTINEL = "looksLikeVisionQueryForPreDispatch"


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if SENTINEL in src:
        print(f"[prelim-lookatme] {path.name} already patched.")
        return False

    # Add `time` and `strings` imports.
    if '\t"strings"\n' not in src.split(")", 1)[0]:
        src = re.sub(r'(import \(\n)', r'\1\t"strings"\n', src, count=1)
    if '\t"time"\n' not in src.split(")", 1)[0]:
        src = re.sub(r'(import \(\n)', r'\1\t"time"\n', src, count=1)

    # Insert dispatch right after the ProcessTextAll line.
    anchor = "\tsuccessMatched = ttr.ProcessTextAll(req, transcribedText, vars.IntentList, speechReq.IsOpus)\n"
    if anchor not in src:
        print(f"[prelim-lookatme] anchor not found in {path}", file=sys.stderr)
        sys.exit(1)
    insert = anchor + """
\t// Before the LLM replies, turn Vector to face whoever just spoke — his
\t// firmware still has the fresh mic-direction cache from the just-finished
\t// voice command and rapid-turns toward the user (same mechanism as
\t// intent_imperative_come). Skipped when he's on the charger: he must not
\t// drive off his pod. Dispatching the intent closes the chipper voice
\t// stream, but StreamingKGSim's response goes through the SDK
\t// (robot.Conn.SayText), so the LLM answer still reaches the user.
\tif !successMatched && !ttr.IsOnCharger() {
\t\tvisionQuery := looksLikeVisionQueryForPreDispatch(transcribedText)
\t\tfmt.Println("[face-speaker] turning Vector to face the speaker")
\t\tttr.IntentPass(req, "intent_imperative_lookatme", transcribedText, map[string]string{}, false)
\t\tif visionQuery {
\t\t\t// Vision: pause so he faces the user before describing the scene.
\t\t\ttime.Sleep(700 * time.Millisecond)
\t\t}
\t}
"""
    src = src.replace(anchor, insert, 1)

    # Make sure fmt is imported (for our Println).
    if '\t"fmt"\n' not in src.split(")", 1)[0]:
        src = re.sub(r'(import \(\n)', r'\1\t"fmt"\n', src, count=1)

    # Append the helper function.
    if "func looksLikeVisionQueryForPreDispatch" not in src:
        helper = '''
// looksLikeVisionQueryForPreDispatch returns true for utterances that benefit
// from Vector facing the user before the LLM responds.
func looksLikeVisionQueryForPreDispatch(text string) bool {
\tt := strings.ToLower(text)
\tneedles := []string{
\t\t"what do you see", "what can you see", "what did you see", "what are you looking at",
\t\t"what you see", "you see this", "you see that", "you see anything",
\t\t"can you see", "see this", "see that",
\t\t"look at this", "look at that", "look at me", "look around", "have a look",
\t\t"what's this", "what's that", "what is this", "what is that",
\t\t"whats this", "whats that",
\t\t"what's on my", "what is on my", "whats on my",
\t\t"how do i look", "how does this look", "how does that look", "do i look",
\t\t"describe this", "describe that", "tell me about this", "tell me about that",
\t\t"check this out", "check that out",
\t}
\tfor _, n := range needles {
\t\tif strings.Contains(t, n) {
\t\t\treturn true
\t\t}
\t}
\treturn false
}
'''
        src = src.rstrip() + "\n" + helper

    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[prelim-lookatme] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-intent_graph.go>", file=sys.stderr)
        sys.exit(2)
    target = Path(sys.argv[1])
    patch(target)
