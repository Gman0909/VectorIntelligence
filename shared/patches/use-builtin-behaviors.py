#!/usr/bin/env python3
"""Route specific "animation" names to Vector's built-in behaviours via AppIntent.

Several entries in Wire-Pod's animationMap reference anim names that don't
actually exist on Vector's firmware (notably `anim_dance_bobbing_01`). Instead
of trying to find a single anim that matches, special-case these in
DoPlayAnimation* to fire Vector's full built-in behaviour (the choreographed
dance, etc.) via AppIntent — same mechanism Wire-Pod already uses for
`knowledge_question`.

Currently mapped:
    dance         -> intent_imperative_dance
    lookAtUser    -> intent_imperative_lookatme

Idempotent.
"""
import re
import sys
from pathlib import Path


SPECIAL = {
    "dance":      "intent_imperative_dance",
    "lookAtUser": "intent_imperative_lookatme",
}


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if "BuiltinBehaviorIntent" in src:
        print(f"[builtin-behaviors] {path.name} already patched.")
        return False

    # Helper map literal + lookup function inserted just before DoPlayAnimation.
    insert = "// BuiltinBehaviorIntent maps animation aliases to Vector firmware\n"
    insert += "// intents so they trigger the full built-in behaviour rather than a\n"
    insert += "// (possibly nonexistent) single animation clip.\n"
    insert += "var BuiltinBehaviorIntent = map[string]string{\n"
    for alias, intent in SPECIAL.items():
        insert += f'\t"{alias}": "{intent}",\n'
    insert += "}\n\n"
    insert += "func tryBuiltinBehavior(name string, robot *vector.Vector) bool {\n"
    insert += "\tif intent, ok := BuiltinBehaviorIntent[name]; ok {\n"
    insert += '\t\tlogger.Println("Triggering built-in behaviour for " + name + " via AppIntent: " + intent)\n'
    insert += "\t\tgo func() {\n"
    insert += "\t\t\trobot.Conn.AppIntent(context.Background(), &vectorpb.AppIntentRequest{Intent: intent})\n"
    insert += "\t\t}()\n"
    insert += "\t\treturn true\n"
    insert += "\t}\n"
    insert += "\treturn false\n"
    insert += "}\n\n"

    # Insert before "func DoPlayAnimation"
    new_src, n = re.subn(
        r"(func DoPlayAnimation\(animation string)",
        insert + r"\g<1>",
        src,
        count=1,
    )
    if n != 1:
        print(f"[builtin-behaviors] DoPlayAnimation anchor not found", file=sys.stderr)
        sys.exit(1)

    # Add the early-return into DoPlayAnimation and DoPlayAnimationWI.
    for fn_name in ("DoPlayAnimation", "DoPlayAnimationWI"):
        anchor = f"func {fn_name}(animation string, robot *vector.Vector) error {{\n"
        if anchor not in new_src:
            print(f"[builtin-behaviors] body of {fn_name} not found", file=sys.stderr)
            sys.exit(1)
        replacement = (
            anchor
            + "\tif tryBuiltinBehavior(animation, robot) {\n"
            + "\t\treturn nil\n"
            + "\t}\n"
        )
        new_src = new_src.replace(anchor, replacement, 1)

    path.write_text(new_src, encoding="utf-8", newline="\n")
    print(f"[builtin-behaviors] {path.name} patched ({len(SPECIAL)} mappings).")
    return True


if __name__ == "__main__":
    target = Path(sys.argv[1])
    patch(target)
