#!/usr/bin/env python3
"""Remove the photo-taking ceremony from Wire-Pod's getImage flow.

Upstream Wire-Pod has DoGetImage:
  1. Turn the screen into a live camera viewfinder
  2. Verbally count down "3", "2", "1"
  3. Take the photo

Cute, but slow and audible — and the countdown is Vector speaking through
his own mic, which trips the wake-word interrupt. Pi version pre-dates this
feature and just takes the photo silently. This patch removes the viewfinder
+ countdown so Windows behaves like the working Pi build.

Idempotent.
"""
import re
import sys
from pathlib import Path


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if 'for i := 3; i > 0; i--' not in src:
        print(f"[remove-countdown] {path.name} already patched (no countdown loop found).")
        return False

    # Match the full viewfinder + countdown block explicitly. DurationScalar
    # value is `[0-9.]+` so we work regardless of whether slow-tts has already
    # run (which would change 0.95 to 1.1).
    block_re = re.compile(
        r"\trobot\.Conn\.EnableMirrorMode\(context\.Background\(\), &vectorpb\.EnableMirrorModeRequest\{\n"
        r"\t\tEnable: true,\n"
        r"\t\}\)\n"
        r"\tfor i := 3; i > 0; i-- \{\n"
        r"\t\tif stopImaging \{\n"
        r"\t\t\treturn\n"
        r"\t\t\}\n"
        r"\t\ttime\.Sleep\(time\.Millisecond \* 300\)\n"
        r"\t\trobot\.Conn\.SayText\(\n"
        r"\t\t\tcontext\.Background\(\),\n"
        r"\t\t\t&vectorpb\.SayTextRequest\{\n"
        r"\t\t\t\tText:           fmt\.Sprint\(i\),\n"
        r"\t\t\t\tUseVectorVoice: true,\n"
        r"\t\t\t\tDurationScalar: [0-9.]+,\n"
        r"\t\t\t\},\n"
        r"\t\t\)\n"
        r"\t\tif stopImaging \{\n"
        r"\t\t\treturn\n"
        r"\t\t\}\n"
        r"\t\}\n"
    )

    new_src, n = block_re.subn(
        "\t// Photo viewfinder + countdown removed - take the picture silently.\n",
        src,
        count=1,
    )
    if n != 1:
        print(f"[remove-countdown] anchor not found in {path}", file=sys.stderr)
        sys.exit(1)
    path.write_text(new_src, encoding="utf-8", newline="\n")
    print(f"[remove-countdown] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path>", file=sys.stderr)
        sys.exit(2)
    target = Path(sys.argv[1])
    patch(target)
