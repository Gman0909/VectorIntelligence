#!/usr/bin/env python3
"""Dispatch intent_imperative_lookatme to Vector via IntentPass BEFORE calling
the LLM, on vision queries. Vector's firmware uses his still-fresh
sound-direction cache to rapid-turn toward the user. After dispatch, the
chipper voice stream is closed (IsFinal=true), but StreamingKGSim's
subsequent speech goes through the SDK (robot.Conn.SayText) so the LLM
response still reaches the user.

Side note: StreamingKGSim's internal call to IntentPass(intent_greeting_hello)
will fail to send (stream is closed), but the error is ignored and the
SDK-based speech proceeds normally.

Modifies preqs/intent_graph.go.

Idempotent.
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
\t// If the user asked a vision question and no built-in intent claimed it,
\t// dispatch intent_imperative_lookatme NOW (before the LLM call). Vector's
\t// firmware still has his fresh mic-direction cache from the just-finished
\t// voice command and will rapid-turn toward the user — same mechanism as
\t// intent_imperative_come. After this, the chipper voice stream closes,
\t// but StreamingKGSim's subsequent response goes through the SDK
\t// (robot.Conn.SayText) so the LLM answer still reaches the user.
\tif !successMatched && looksLikeVisionQueryForPreDispatch(transcribedText) {
\t\tfmt.Println("[prelim-lookatme] vision query detected — dispatching intent_imperative_lookatme")
\t\tttr.IntentPass(req, "intent_imperative_lookatme", transcribedText, map[string]string{}, false)
\t\t// Brief pause so Vector starts turning before the SDK speech kicks in.
\t\ttime.Sleep(700 * time.Millisecond)
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
