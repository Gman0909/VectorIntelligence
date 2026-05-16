#!/usr/bin/env python3
"""Expand Wire-Pod's LLM-accessible animation vocabulary.

Adds entries to animationMap in kgsim_cmds.go and updates the ParamChoices
strings for playAnimation / playAnimationWI so Wire-Pod's auto-generated
prompt advertises them to the LLM.

Robust to upstream changes — finds the animationMap slice by its declaration
header rather than a specific last entry. Restores 'dance' if upstream
removed it.

Idempotent.
"""
import re
import sys
from pathlib import Path

NEW_ANIMS = [
    ("fistBump",    "anim_fistbump_success_01"),
    ("hello",       "anim_greeting_hello_01"),
    ("goodbye",     "anim_greeting_goodbye_01"),
    ("goodMorning", "anim_greeting_goodmorning_01"),
    ("goodNight",   "anim_greeting_goodnight_01"),
    ("purr",        "anim_petting_blissloop_01"),
    ("huh",         "anim_explorer_huh_close_01"),
    ("smile",       "anim_eyecontact_smile_01"),
    ("wakeup",      "anim_onboarding_wakeword_getin_01"),
    ("look",        "anim_attention_lookatdevice_01"),
    # Re-add dance in case upstream dropped it. The actual playback is
    # intercepted by use-builtin-behaviors.py and routed to Vector's
    # firmware via AppIntent instead.
    ("dance",       "anim_dance_bobbing_01"),
    # lookAtUser: same trick — routed to AppIntent(imperative_lookatme).
    # The anim name here is a placeholder; tryBuiltinBehavior catches it
    # before any animation playback would happen.
    ("lookAtUser",  "anim_attention_lookatdevice_01"),
]

BASE_PARAMS = [
    "happy", "veryHappy", "sad", "verySad", "angry", "frustrated",
    "dartingEyes", "confused", "thinking", "celebrate", "love",
]


def patch_file(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if "fistBump" in src and "anim_fistbump_success_01" in src:
        print(f"[expand-animations] {path.name} already patched.")
        return False

    # Match the animationMap slice from its declaration through the closing
    # brace of the slice literal. The `(?:\n[^}].*)*` makes us greedy through
    # all the interior `}`s (entry closures) but stop at the outer `}`.
    slice_re = re.compile(
        r'(var animationMap \[\]\[2\]string = \[\]\[2\]string\{\n'
        r'(?:.+\n)*?'    # entries (non-greedy)
        r')\}',
    )
    m = slice_re.search(src)
    if not m:
        print(f"[expand-animations] animationMap slice not found", file=sys.stderr)
        sys.exit(1)

    existing_body = m.group(1)
    additions = ""
    for name, anim in NEW_ANIMS:
        if f'"{name}"' in existing_body:
            continue   # already in the map
        additions += f'\t{{\n\t\t"{name}",\n\t\t"{anim}",\n\t}},\n'

    if not additions:
        print(f"[expand-animations] all animations already present.")
        return False

    new_slice = existing_body + additions + "}"
    src = src[:m.start()] + new_slice + src[m.end():]

    # Rewrite ParamChoices for the playAnimation* entries to advertise our
    # expanded list. We rewrite every ParamChoices line that mentions one of
    # the BASE_PARAMS values (i.e. the animation commands, not other commands).
    all_params = list(BASE_PARAMS) + [n for n, _ in NEW_ANIMS if n not in BASE_PARAMS]
    expanded = ", ".join(all_params)
    src = re.sub(
        r'(ParamChoices:\s+")([^"]*\bhappy\b[^"]*)(")',
        rf'\g<1>{expanded}\g<3>',
        src,
    )

    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[expand-animations] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path>", file=sys.stderr)
        sys.exit(2)
    target = Path(sys.argv[1])
    patch_file(target)
