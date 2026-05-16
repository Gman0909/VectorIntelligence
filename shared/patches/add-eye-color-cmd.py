#!/usr/bin/env python3
"""Add a `{{eyeColor||<name>}}` command to Wire-Pod's LLM command vocabulary.

Lets the LLM shift Vector's eye colour to express mood. Maps short colour
names (matching the firmware presets) to vectorpb.EyeColor enum values, then
issues an UpdateSettings gRPC call.

Idempotent. Modifies chipper/pkg/wirepod/ttr/kgsim_cmds.go.
"""
import re
import sys
from pathlib import Path

SENTINEL = "ActionSetEyeColor"


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if SENTINEL in src:
        print(f"[eye-color] {path.name} already patched.")
        return False

    # 1. Add action constant.
    src = re.sub(
        r'(ActionPlaySound = 4\n\))',
        r'ActionPlaySound = 4\n'
        r'\t// arg: colour name (teal/orange/yellow/lime/blue/purple/green)\n'
        r'\tActionSetEyeColor = 5\n)',
        src,
        count=1,
    )

    # 2. Add command entry to ValidLLMCommands (insert after newVoiceRequest).
    newcmd = '''\t{
\t\tCommand:         "eyeColor",
\t\tDescription:     "Shift your eye colour to match your mood. Use when your response has a distinct emotional tone (sardonic, amused, mischievous, thoughtful, etc.) — roughly half your responses should set a colour. Place this command at the START of your response.",
\t\tParamChoices:    "teal, orange, yellow, lime, blue, purple, green",
\t\tAction:          ActionSetEyeColor,
\t\tSupportedModels: []string{"all"},
\t},
'''
    anchor = (
        '\t{\n'
        '\t\tCommand:         "newVoiceRequest",\n'
    )
    if anchor not in src:
        print(f"[eye-color] anchor for command list not found", file=sys.stderr)
        sys.exit(1)
    # Insert AFTER the newVoiceRequest block — find its closing `},` and place ours after.
    # Locate the entire newVoiceRequest entry.
    cmd_block_re = re.compile(
        r'(\t\{\n\t\tCommand:\s*"newVoiceRequest",.*?\n\t\},\n)',
        re.DOTALL,
    )
    m = cmd_block_re.search(src)
    if not m:
        print(f"[eye-color] could not find newVoiceRequest command block", file=sys.stderr)
        sys.exit(1)
    src = src[:m.end()] + newcmd + src[m.end():]

    # 3. Add helper + action handler function. Place before `func PerformActions`.
    helper = '''
// colourNameToPreset maps the short colour names exposed to the LLM to
// Vector's firmware preset eye colour index.
func colourNameToPreset(name string) int {
\tswitch strings.ToLower(strings.TrimSpace(name)) {
\tcase "teal":
\t\treturn 0 // TIP_OVER_TEAL
\tcase "orange":
\t\treturn 1 // OVERFIT_ORANGE
\tcase "yellow":
\t\treturn 2 // UNCANNY_YELLOW
\tcase "lime":
\t\treturn 3 // NON_LINEAR_LIME
\tcase "blue":
\t\treturn 4 // SINGULARITY_SAPPHIRE
\tcase "purple":
\t\treturn 5 // FALSE_POSITIVE_PURPLE
\tcase "green":
\t\treturn 6 // CONFUSION_MATRIX_GREEN
\t}
\treturn 0
}

// DoSetEyeColor sets Vector's eye colour to the named preset via the HTTPS
// settings endpoint. This is the same path Wire-Pod's web UI uses — the gRPC
// UpdateSettings only exposes the preset field, but firmware ignores it if
// custom eye colour is enabled, so we must disable that here too.
func DoSetEyeColor(name string, robot *vector.Vector) error {
\tpreset := colourNameToPreset(name)
\turl := "https://" + robot.Cfg.Target + "/v1/update_settings"
\tbody := []byte(fmt.Sprintf(`{"update_settings": true, "settings": {"custom_eye_color": {"enabled": false}, "eye_color": %d} }`, preset))
\treq, err := http.NewRequest("POST", url, bytes.NewBuffer(body))
\tif err != nil {
\t\tfmt.Printf("[eye-color] NewRequest failed: %v\\n", err)
\t\treturn err
\t}
\treq.Header.Set("Authorization", "Bearer "+robot.Cfg.Token)
\treq.Header.Set("Content-Type", "application/json")
\ttr := &http.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: true}}
\tclient := &http.Client{Transport: tr, Timeout: 5 * time.Second}
\tresp, err := client.Do(req)
\tif err != nil {
\t\tfmt.Printf("[eye-color] POST %s failed: %v\\n", url, err)
\t\treturn err
\t}
\tdefer resp.Body.Close()
\tfmt.Printf("[eye-color] set eyes to %s (preset %d), status: %s\\n", name, preset, resp.Status)
\treturn nil
}

'''
    anchor = "func PerformActions("
    if anchor not in src:
        print(f"[eye-color] could not find PerformActions function", file=sys.stderr)
        sys.exit(1)
    idx = src.index(anchor)
    src = src[:idx] + helper + src[idx:]

    # 4. Add case in the PerformActions switch.
    switch_anchor = (
        "\t\tcase action.Action == ActionPlaySound:\n"
        "\t\t\tDoPlaySound(action.Parameter, robot)\n"
    )
    if switch_anchor not in src:
        print(f"[eye-color] PerformActions switch anchor not found", file=sys.stderr)
        sys.exit(1)
    new_case = (
        switch_anchor
        + "\t\tcase action.Action == ActionSetEyeColor:\n"
        + "\t\t\tDoSetEyeColor(action.Parameter, robot)\n"
    )
    src = src.replace(switch_anchor, new_case, 1)

    # 5. Make sure required imports are present.
    needed = ['"strings"', '"bytes"', '"crypto/tls"', '"net/http"', '"time"']
    import_block_end = src.index(")", src.index("import ("))
    import_block = src[:import_block_end]
    for imp in needed:
        if f'\t{imp}\n' not in import_block:
            src = re.sub(r'(import \(\n)', r'\1\t' + imp + '\n', src, count=1)
            import_block_end += len(imp) + 2
            import_block = src[:import_block_end]

    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[eye-color] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-kgsim_cmds.go>", file=sys.stderr)
        sys.exit(2)
    patch(Path(sys.argv[1]))
