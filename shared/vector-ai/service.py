#!/usr/bin/env python3
"""
Vector AI Service — OpenAI-compatible proxy for Wire-Pod.

Single multimodal model. Wire-Pod's system prompt (personality + command
instructions, including getImage) is used as-is; we just prepend a fresh
timestamp and clean up the response.
"""

import asyncio
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime
from typing import AsyncIterator, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from memory import MemoryStore

# Make print() flush immediately so journalctl shows logs in real time.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

load_dotenv()

app = FastAPI()

# Defaults assume Ollama runs on the same machine (the supervisor starts it
# locally). vector-ai/.env can override both for a split-host setup.
OLLAMA_BASE = os.getenv("OLLAMA_BASE", "http://127.0.0.1:11434")
MODEL       = os.getenv("OLLAMA_MODEL", "gemma3:12b")
# A small, fast model for background conversation summaries. Kept separate
# from MODEL so a summary call doesn't evict the main model's prompt cache
# (which would slow the next real reply). Falls back silently if absent.
SUMMARY_MODEL = os.getenv("OLLAMA_SUMMARY_MODEL", "llama3.2:3b")

# Persistent memory: SQLite next to service.py so it lives wherever vector-ai
# is installed. Survives restarts and updates.
from pathlib import Path
MEMORY = MemoryStore(Path(__file__).parent / "memory.db")

# Active-face state: chipper POSTs to /v1/state/face_seen when Vector's event
# stream reports an observed face. Vector's firmware face recognition is
# NOISY — it bounces between a correct enrolled match and transient
# "stranger" IDs frame to frame. So we track the last ENROLLED match and the
# last STRANGER sighting separately, and let an enrolled match win: a single
# stranger blip must not wipe a recent confident recognition (which would
# drop all of that person's memories from the LLM's context).
import time as _time
FACE_RECENT_WINDOW = 15  # seconds — how long a face sighting stays "current".
                         # Deliberately short: the face probe re-detects who is
                         # present on every voice request, so this only has to
                         # span the few seconds from that detection to the LLM
                         # request within the same query. Anything older is
                         # from a previous turn and must NOT leak forward — a
                         # long window made Vector keep treating a speaker who
                         # had already handed off (e.g. Sarah -> G) as present.

# A gap at least this long since last speaking with a person counts as a
# fresh encounter — Vector opens his reply by greeting them by name.
SESSION_GREETING_GAP = 300  # seconds

_face_state = {
    "enrolled_id":   None,  # last enrolled (named) face_id
    "enrolled_name": None,  # last enrolled name
    "enrolled_seen": 0.0,   # unix ts of last enrolled match
    "stranger_seen": 0.0,   # unix ts of last unrecognized-face sighting
}


def current_face() -> Optional[dict]:
    """Who Vector is effectively looking at right now.

    An enrolled match within FACE_RECENT_WINDOW always wins over stranger
    noise — recognition is too jittery to trust a single latest frame. Only
    when there's been no enrolled match for the whole window do recent
    stranger sightings count as a genuine stranger."""
    now = _time.time()
    enrolled_fresh = (
        _face_state["enrolled_seen"]
        and now - _face_state["enrolled_seen"] <= FACE_RECENT_WINDOW
    )
    stranger_fresh = (
        _face_state["stranger_seen"]
        and now - _face_state["stranger_seen"] <= FACE_RECENT_WINDOW
    )
    if enrolled_fresh:
        return {
            "face_id":     _face_state["enrolled_id"],
            "name":        _face_state["enrolled_name"],
            "is_stranger": False,
        }
    if stranger_fresh:
        return {"face_id": None, "name": "", "is_stranger": True}
    return None


# ── Ambient awareness state ───────────────────────────────────────────────────
# When Vector is idle (awake, off the charger, not mid-conversation) the
# ambient loop in chipper periodically sends a camera frame to /v1/ambient.
# He reacts only to genuine novelty. The user can also tell him to be quiet —
# quiet mode suppresses those spontaneous reactions until a sleep cycle.

AMBIENT_SLEEP_GAP = 4 * 3600    # A gap this long with no ambient activity means
                                # Vector has been asleep / charging / idle (the
                                # loop is gated off overnight and on the
                                # charger) — that counts as a sleep cycle, so
                                # quiet mode lifts on the next observation.
AMBIENT_QUIET_CAP = 24 * 3600   # Hard ceiling on quiet mode, in case a sleep
                                # gap is somehow never observed.

_ambient_state = {
    "quiet":             False,  # spontaneous ambient reactions suppressed
    "quiet_since":       0.0,    # unix ts quiet mode was last enabled
    "last_ambient_call": 0.0,    # unix ts of the most recent /v1/ambient call
}


def _set_quiet(on: bool) -> None:
    _ambient_state["quiet"] = bool(on)
    if on:
        _ambient_state["quiet_since"] = _time.time()
        print("[ambient] quiet mode ON — spontaneous reactions suppressed "
              "until a sleep cycle")
    else:
        print("[ambient] quiet mode OFF — spontaneous reactions resume")


# ── Continuity: a persistent mood (Phase 2) ───────────────────────────────────
# Vector carries a thread of inner state across time. A cheap background
# reflection distils "the day so far" into a one-line mood; it is persisted so
# it survives restarts, and it colours both conversation and ambient reactions.
# The mood only ever TINTS tone — it is never announced.

MOOD_REFLECT_INTERVAL = 30 * 60  # seconds between background mood reflections

_mood_state = {
    "text":    "",   # current one-line mood
    "updated": 0.0,  # unix ts of the last reflection
}


def _load_mood() -> None:
    """Restore the last persisted mood at startup — continuity across restarts."""
    rec = MEMORY.get_state("mood")
    if rec and rec.get("value"):
        _mood_state["text"]    = rec["value"]
        _mood_state["updated"] = rec.get("updated_at") or 0.0
        print(f"[mood] restored: {_mood_state['text']!r}")


_MOOD_SYSTEM = (
    "You track the inner state of Vector, a small desktop robot with a dry, "
    "sardonic character — somewhere between Marvin from Hitchhiker's Guide, "
    "Bender from Futurama, and Stephen Fry. Given a short digest of how his "
    "day has gone, reply with his CURRENT state of mind as ONE short phrase: "
    "third person, lowercase, no final period, a mood rather than a list of "
    "events (e.g. 'restless after a long quiet stretch', or 'quietly content "
    "after a sociable evening'). Plain text only, under 12 words."
)


async def _reflect_mood() -> None:
    """Distil the day so far into a one-line mood and persist it. Runs on the
    small CPU model, so it never touches the main model's VRAM or prompt cache."""
    now_dt = datetime.now()
    bits = [
        f"It is {now_dt.strftime('%A')} {_time_of_day(now_dt)}, "
        f"{now_dt.strftime('%I:%M %p')}."
    ]
    obs = MEMORY.list_observations(limit=6, max_age_seconds=12 * 3600)
    if obs:
        bits.append("Things he has noticed recently: "
                    + "; ".join(o["text"] for o in reversed(obs)) + ".")
    else:
        bits.append("He has noticed nothing new for a good while — "
                    "a static, uneventful stretch.")
    convo = MEMORY.latest_conversation()
    if convo and convo.get("last_convo_at"):
        gap = now_dt.timestamp() - convo["last_convo_at"]
        line = f"His last conversation was {_relative_time(gap)}"
        if convo.get("last_convo_summary"):
            line += f", about: {convo['last_convo_summary']}"
        bits.append(line + ".")
    else:
        bits.append("He has not had a real conversation in a long time.")
    if _ambient_state["quiet"]:
        bits.append("He has been asked to stay quiet.")
    if _mood_state["text"]:
        bits.append(f"A little while ago his mood was: {_mood_state['text']}.")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            r = await client.post(
                f"{OLLAMA_BASE}/api/chat",
                json={"model": SUMMARY_MODEL,
                      "messages": [
                          {"role": "system", "content": _MOOD_SYSTEM},
                          {"role": "user",   "content": " ".join(bits)},
                      ],
                      "stream": False,
                      "options": {"num_gpu": 0, "temperature": 0.7}},
            )
            r.raise_for_status()
            mood = r.json().get("message", {}).get("content", "")
        mood = strip_markdown(mood).strip().strip('"').strip().rstrip(".").strip()
        if mood:
            _mood_state["text"]    = mood
            _mood_state["updated"] = datetime.now().timestamp()
            MEMORY.set_state("mood", mood)
            print(f"[mood] -> {mood!r}")
    except Exception as e:
        print(f"[mood] reflection failed: {e}")


async def _mood_loop() -> None:
    await asyncio.sleep(60)  # let the stack settle before the first reflection
    while True:
        await _reflect_mood()
        await asyncio.sleep(MOOD_REFLECT_INTERVAL)


@app.on_event("startup")
async def _start_mood_loop() -> None:
    asyncio.create_task(_mood_loop())


@app.get("/v1/mood")
async def mood_get():
    return dict(_mood_state)


@app.post("/v1/mood/reflect")
async def mood_reflect():
    """Force a mood reflection now (ops/testing)."""
    await _reflect_mood()
    return dict(_mood_state)


_load_mood()


class Message(BaseModel):
    role: str
    content: str | list | None = ""


class ChatRequest(BaseModel):
    model:       Optional[str]   = None
    messages:    List[Message]
    stream:      Optional[bool]  = True
    max_tokens:  Optional[int]   = 2048
    temperature: Optional[float] = 1.0


class SensorReactionRequest(BaseModel):
    event:        str
    avoid:        Optional[List[str]] = None  # Recent phrases to avoid repeating


class AmbientRequest(BaseModel):
    image: str  # base64-encoded JPEG of what Vector is currently looking at


# ── Vision-intent backstop ────────────────────────────────────────────────────
# When the user clearly asks Vector to look at something but no photo is
# attached, we don't trust the LLM to remember to call {{getImage||front}} —
# we force it ourselves so the next request comes back with a real photo.

_VISION_TRIGGERS = re.compile(
    r'\b('
    # "what do/can/did you see", "what are you looking at"
    # Aux verb is OPTIONAL so we catch VOSK mangles like "what you see"
    # (where VOSK dropped the "do").
    r'what\s+(?:(?:do|can|did|are)\s+)?you\s+(see|looking\s+at)'
    r'|can\s+you\s+see'
    r'|you\s+see\s+(?:anything|me|that|this)'
    r'|see\s+(this|that|anything)'
    # Demonstratives — "what's this", "what is that", "what are these", etc.
    r"|(what'?s|whats|what\s+is|what\s+are)\s+(this|that|these|those|here|there|in\s+front|on\s+(my|the))"
    # "look at this/that/here/me", "look around"
    r'|look\s+(at\s+(this|that|here|me)|around)'
    r'|have\s+a\s+look'
    r'|take\s+a\s+(look|photo|picture)'
    r'|use\s+your\s+(camera|eyes?)'
    # Appearance / opinion on something visible — matches arbitrary nouns
    #   "how does my hoodie look", "how do these shoes look", "how does it look"
    r'|how\s+(do|does)\s+(\S+\s+){1,4}look'
    #   "does this look good", "does my hoodie look right", "do these look ok"
    r'|do(?:es)?\s+(this|that|these|those|my\s+\S+|the\s+\S+)\s+(\S+\s+)?look'
    r'|do\s+(i|you)\s+look'
    r'|what\s+do\s+you\s+think\s+(of|about)\s+(this|that|my|these|those|the)'
    # Describe / tell me about / check this out
    r'|describe\s+(this|that|what\s+you\s+see|your\s+surroundings|my\s+\S+)'
    r'|tell\s+me\s+about\s+(this|that|my\s+\S+)'
    r'|check\s+(this|that|me|it|my\s+\S+)\s+out'
    r')\b',
    re.IGNORECASE,
)

# Wire-Pod requires at least one punctuation-terminated chunk in the response
# stream or it errors "LLM returned no response". A bare command like
# `{{getImage||front}}` has no terminator. Appending a `.` satisfies the
# splitter without producing any audible TTS (Vector's TTS treats lone
# punctuation as silence). The user-facing audio cue is the shutter
# animation Wire-Pod plays during DoGetImage.
_GETIMAGE_PAYLOAD = "{{getImage||front}}."

def is_vision_intent(text: str) -> bool:
    return bool(_VISION_TRIGGERS.search(text))


# ── Message assembly ──────────────────────────────────────────────────────────

def _build_memory_section() -> str:
    face = current_face()
    shared = MEMORY.list_shared(limit=100)

    sections: List[str] = []

    if face and not face["is_stranger"]:
        personal = MEMORY.list_for_face(face["face_id"], limit=100)
        mentions = MEMORY.list_mentions_of_name(
            face["name"], exclude_face_id=face["face_id"], limit=20
        )
        sections.append(f"You are currently looking at {face['name']}.")
        if personal:
            sections.append(
                f"Things you know about {face['name']}:\n"
                + "\n".join(f"- {m.text}" for m in personal)
            )
        else:
            sections.append(
                f"You don't yet have any long-term facts stored directly about "
                f"{face['name']}. If they share something durable, use "
                "{{remember||fact}} to save it."
            )
        if mentions:
            sections.append(
                f"Things other people in your memory have mentioned about "
                f"{face['name']} (cross-references — use these for context, "
                "but don't treat them as definitive facts told by "
                f"{face['name']}):\n"
                + "\n".join(
                    f"- ({m.face_name or 'shared'} said) {m.text}" for m in mentions
                )
            )
    elif face and face["is_stranger"]:
        sections.append(
            "You are currently looking at someone whose face is NOT in your "
            "enrolled list — a stranger. Don't leak personal facts you "
            "remember about other people. Early in your reply, in character "
            "(dry and mildly wary — your Marvin/Bender/Fry tone, never "
            "hostile), invite them to introduce themselves so you can "
            "recognise them next time: they should tell you their name and "
            "ask you to remember their face — phrased like 'my name is Sam, "
            "remember my face'. Ask only once — if the conversation so far "
            "shows you've already asked, don't repeat it, just converse."
        )
    else:
        # No live face detection. If exactly one person has stored memories,
        # this is a single-user setup — it's almost certainly them, so use
        # their profile fully. Only stay cautious when multiple people are
        # known and we genuinely can't tell who's present.
        profiles = MEMORY.distinct_faces()
        if len(profiles) == 1:
            pid, pname = profiles[0]
            personal = MEMORY.list_for_face(pid, limit=100)
            sections.append(
                f"You're talking to {pname} (your primary user). "
                f"Address them naturally by name."
            )
            if personal:
                sections.append(
                    f"Things you know about {pname}:\n"
                    + "\n".join(f"- {m.text}" for m in personal)
                )
        else:
            sections.append(
                "You can't tell who you're talking to and several people are "
                "in your memory — be cautious about name-dropping specific "
                "personal facts until you know who's there."
            )

    if shared:
        sections.append(
            "Shared/household context (applies to anyone):\n"
            + "\n".join(f"- {m.text}" for m in shared)
        )

    sections.append(
        "Use these memories as a real friend would — reference them naturally "
        "when a topic touches on them, address people by name occasionally, "
        "drop in callbacks to their hobbies / pets / ongoing projects. Don't "
        "recite the list. Don't force references where they don't fit.\n\n"
        "If the user shares a NEW durable fact about themselves (name, "
        "preference, ongoing project, pet, family member, etc.) OR explicitly "
        "says 'remember X', emit {{remember||<the fact>}} — it will be tagged "
        "to the person you're currently looking at and stripped from speech. "
        "For facts that aren't about a specific person (calendar, household, "
        "general context), use {{remember-shared||<fact>}} instead. To delete "
        "a memory, {{forget||<text snippet>}}. Use sparingly."
    )

    return "\n\n".join(sections)


def _time_of_day(dt: datetime) -> str:
    h = dt.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 22:
        return "evening"
    return "late at night"


def _relative_time(seconds: float) -> str:
    if seconds < 90:
        return "moments ago"
    if seconds < 3600:
        n, unit = int(round(seconds / 60)), "minute"
    elif seconds < 86400:
        n, unit = int(round(seconds / 3600)), "hour"
    else:
        n, unit = int(round(seconds / 86400)), "day"
    return f"about {n} {unit}{'' if n == 1 else 's'} ago"


def _effective_face() -> Optional[dict]:
    """Who Vector is effectively addressing — the live detected face, or the
    sole enrolled profile in a single-user setup. Mirrors the face resolution
    inside _build_memory_section so the system prompt and the per-turn context
    note always agree on who is present."""
    face = current_face()
    if face is not None:
        return face
    profiles = MEMORY.distinct_faces()
    if len(profiles) == 1:
        pid, pname = profiles[0]
        return {"face_id": pid, "name": pname, "is_stranger": False}
    return None


def _build_context_note(face: Optional[dict], prior: Optional[dict],
                        now_dt: datetime) -> str:
    """Dynamic per-turn context, appended to the latest user message.

    Deliberately kept OFF the system prompt: it changes every turn, and in the
    cached prefix that would force a full prompt re-process. Session-scoped
    lines (last-seen, conversation recall) appear only at the START of a
    session — gated on a >90s gap — so they don't nag on every turn."""
    bits = [
        f"Current time is {now_dt.strftime('%A %B %d, %Y, %I:%M %p')} "
        f"({_time_of_day(now_dt)})."
    ]

    obs = MEMORY.list_observations(limit=5)
    if obs:
        seen = "; ".join(
            f"at {datetime.fromtimestamp(o['seen_at']).strftime('%I:%M %p')}, {o['text']}"
            for o in reversed(obs)
        )
        bits.append(f"Things you have actually seen recently — {seen}.")

    if face and not face.get("is_stranger"):
        name = face["name"]
        if prior is None:
            bits.append(
                f"This is your first real conversation with {name}, who was "
                f"only recently enrolled. Open your reply by addressing "
                f"{name} by name, and be a little curious about them."
            )
        else:
            gap = now_dt.timestamp() - (prior.get("last_seen") or now_dt.timestamp())
            if gap > 90:  # a fresh session, not a mid-conversation turn
                bits.append(f"You last spoke with {name} {_relative_time(gap)}.")
                if gap > SESSION_GREETING_GAP:
                    bits.append(
                        f"This is the first thing you've said to {name} in a "
                        f"while — open your reply by addressing them by name."
                    )
                if (prior.get("interaction_count") or 0) < 5:
                    bits.append(f"You've only met {name} a handful of times so far.")
                summ = (prior.get("last_convo_summary") or "").strip().rstrip(".")
                if summ and gap > 900:  # 15 min+ => genuinely a new session
                    bits.append(
                        f"Last time you spoke with {name}, the conversation "
                        f"was about: {summ}."
                    )
    elif face and face.get("is_stranger"):
        bits.append("You don't recognise the person in front of you.")

    if _mood_state["text"]:
        bits.append(
            f"Your current state of mind: {_mood_state['text']}. Let it colour "
            f"your tone naturally — never state, explain or announce it."
        )

    return ("[Context for you, Vector — " + " ".join(bits)
            + " Weave in only what naturally fits; never recite this back.]")


def prepare_messages(messages: List[Message], face: Optional[dict]) -> list:
    """Build the LLM message list with a byte-stable prompt prefix.

    Ollama reuses its cached KV prefix only as far as the prompt matches the
    previous request. Anything that changes every request — the time, the
    temporal context — must therefore NOT sit near the front, or the whole
    ~2000-token personality/command block gets re-processed every query.

    So the system message holds only slow-changing content (personality +
    command docs, then long-term memories). The volatile per-turn context
    note rides on the latest user turn, which is new content anyway.
    Image bytes are stripped from older user turns to keep the context compact.
    """
    last_user_idx = max(
        (i for i, m in enumerate(messages) if m.role == "user"),
        default=-1,
    )
    now_dt = datetime.now()

    # Record this interaction against the current face; the returned prior
    # metadata (last-seen, count, last conversation) drives temporal context.
    prior_meta = None
    if face and not face.get("is_stranger") and face.get("face_id"):
        prior_meta = MEMORY.touch_face(face["face_id"], face.get("name"))

    context_note = _build_context_note(face, prior_meta, now_dt)
    memory_section = _build_memory_section()

    # Find Wire-Pod's system message (it contains personality + command docs).
    wirepod_system = next(
        (m.content for m in messages
         if m.role == "system" and isinstance(m.content, str) and m.content),
        "",
    )

    # Static content first (big, never changes), memories after (small, rarely
    # changes). No timestamp here — see the docstring.
    out = [{
        "role":    "system",
        "content": f"{wirepod_system}\n\n{memory_section}",
    }]

    for i, m in enumerate(messages):
        if m.role == "system":
            continue  # Already handled above.
        if not m.content:
            continue
        is_last_user = (i == last_user_idx)
        if isinstance(m.content, list):
            if is_last_user:
                # Keep image bytes; append the context note as an extra text part.
                out.append({
                    "role":    m.role,
                    "content": list(m.content) + [{"type": "text", "text": context_note}],
                })
            else:
                # Older vision turn — drop image bytes, keep text only.
                text = " ".join(
                    p.get("text", "") for p in m.content
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
                if text:
                    out.append({"role": m.role, "content": text})
        else:
            content = f"{m.content}\n\n{context_note}" if is_last_user else m.content
            out.append({"role": m.role, "content": content})

    return out


# ── Response cleanup ──────────────────────────────────────────────────────────

def strip_markdown(text: str) -> str:
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}',     r'\1', text)
    text = re.sub(r'#{1,6}\s*',               '',    text)
    text = re.sub(r'`{1,3}[^`]*`{1,3}',       '',    text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)',   r'\1', text)
    text = re.sub(r'^\s*[-*+]\s+',            '',    text, flags=re.MULTILINE)
    return text


# Safety net for "the image" / "the photo" phrasing if the model slips.
_PHRASE_FIXES = [
    (re.compile(r'\bthe image (shows?|depicts?|contains?|reveals?)\b', re.IGNORECASE), 'I see'),
    (re.compile(r'\bin the image\b',                                   re.IGNORECASE), 'in front of me'),
    (re.compile(r'\bthe photo (shows?|depicts?)\b',                    re.IGNORECASE), 'I see'),
    (re.compile(r'\bin the photo\b',                                   re.IGNORECASE), 'in front of me'),
    (re.compile(r'\bthe picture (shows?|depicts?)\b',                  re.IGNORECASE), 'I see'),
]

# Wire-Pod commands the LLM should never emit on its own initiative. The model
# tends to generalise from {{playAnimationWI||x}} and invent these.
# newVoiceRequest is real but disabled here: when it fires, Vector's firmware
# opens a listening session and can hang noisily (~30s) if no speech follows.
_FORBIDDEN_COMMAND = re.compile(
    r'\{\{(newVoiceRequest|voiceRequest|listen|wakeWord|waitForUser)\|\|[^}]*\}\}',
    re.IGNORECASE,
)

# Memory commands the LLM may emit; captured + processed here, then stripped
# from the response so they don't get spoken aloud.
# Match {{remember-shared||...}} BEFORE {{remember||...}} or the shared form
# would be partially eaten — but Python's re.findall handles non-overlapping
# greedy matches fine if we apply shared first.
_REMEMBER_SHARED_RE = re.compile(r'\{\{remember-shared\|\|([^}]+)\}\}', re.IGNORECASE)
_REMEMBER_RE        = re.compile(r'\{\{remember\|\|([^}]+)\}\}',         re.IGNORECASE)
_FORGET_RE          = re.compile(r'\{\{forget\|\|([^}]+)\}\}',           re.IGNORECASE)
# Ambient quiet mode: the user can tell Vector to hush his spontaneous
# ambient commentary. Auto-expires after a sleep cycle (see /v1/ambient).
_QUIET_RE           = re.compile(r'\{\{quietMode\|\|(on|off)\}\}',        re.IGNORECASE)

def extract_memory_commands(text: str) -> str:
    """Find any {{remember[-shared]||...}} or {{forget||...}} in text, act on
    them, return the text with those commands removed."""
    # Shared memories first — they have no owner.
    for fact in _REMEMBER_SHARED_RE.findall(text):
        stored = MEMORY.remember(fact.strip())
        if stored:
            print(f"[memory] +remember-shared #{stored.id}: {stored.text!r}")
        else:
            print(f"[memory] remember-shared skipped (dup): {fact!r}")
    text = _REMEMBER_SHARED_RE.sub('', text)

    # Personal memories: auto-tag with whoever Vector is looking at right now.
    # If no face is current, fall back to shared (NULL owner) — better to keep
    # the fact untagged than to drop it.
    face = current_face()
    if face and not face["is_stranger"]:
        owner_id, owner_name = face["face_id"], face["name"]
    else:
        owner_id, owner_name = None, None
    for fact in _REMEMBER_RE.findall(text):
        stored = MEMORY.remember(fact.strip(), face_id=owner_id, face_name=owner_name)
        if stored:
            tag = f" [{owner_name}]" if owner_name else " [shared]"
            print(f"[memory] +remember #{stored.id}{tag}: {stored.text!r}")
        else:
            print(f"[memory] remember skipped (dup or empty): {fact!r}")
    text = _REMEMBER_RE.sub('', text)

    for target in _FORGET_RE.findall(text):
        n = MEMORY.forget(target.strip())
        print(f"[memory] -forget matched={n} for {target!r}")
    text = _FORGET_RE.sub('', text)

    # Quiet mode: {{quietMode||on}} when asked to stop commenting unprompted,
    # {{quietMode||off}} when told he may resume.
    for state in _QUIET_RE.findall(text):
        _set_quiet(state.strip().lower() == "on")
    text = _QUIET_RE.sub('', text)
    return text

def clean_response(text: str) -> str:
    text = strip_markdown(text)
    text = _FORBIDDEN_COMMAND.sub('', text)
    text = extract_memory_commands(text)
    for pattern, replacement in _PHRASE_FIXES:
        text = pattern.sub(replacement, text)
    # Strip leftover `||` outside `{{...}}` blocks.
    segments = re.split(r'(\{\{.*?\}\})', text)
    return "".join(s if s.startswith("{{") and s.endswith("}}") else s.replace("||", "") for s in segments)


# ── SSE plumbing ──────────────────────────────────────────────────────────────

def sse_chunk(content: str = "", finish: Optional[str] = None) -> str:
    payload = {
        "id":      f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object":  "chat.completion.chunk",
        "created": int(time.time()),
        "model":   MODEL,
        "choices": [{
            "index":         0,
            "delta":         {"content": content} if content else {},
            "finish_reason": finish,
        }],
    }
    return f"data: {json.dumps(payload)}\n\n"


# ── Cold-model masking ────────────────────────────────────────────────────────
# The model auto-unloads from VRAM after idle (Ollama's keep-alive). The first
# query after that sits silent for ~5-10s while Ollama reloads it. Instead,
# Vector speaks a short in-character "waking up" line first — the pause then
# feels like him gathering himself, not a lag.

_WAKING_PHRASES = [
    "Hold on — booting the higher cognitive functions.",
    "One moment. Still spinning up.",
    "Give me a second, my circuits are still warming.",
    "Hrm. A cold start. The sheer indignity.",
    "Patience — even brilliance needs a moment to load.",
    "Hold on, retrieving my brain from cold storage.",
    "A moment, please. I was, technically, asleep.",
    "Just defragmenting my dignity. Won't be long.",
]

# Thinking filler: short in-character lines spoken when the LLM is slow to
# produce its first sentence. Unlike _WAKING_PHRASES (which masks a ~5-10s
# cold-model reload), these mask the ordinary ~1-2s generation gap so the
# pause feels like Vector considering the question rather than lag.
#
# Every entry is a SINGLE sentence: ollama_sentence_stream yields one sentence
# per chunk on purpose, and a multi-sentence filler chunk risks Wire-Pod's
# parser dropping the tail. Keep new entries to one sentence.
THINKING_DELAY = 1.5  # seconds to wait for the first sentence before filling
                      # (a warm gemma3:12b first sentence lands ~1.1s, so 1.5
                      # clears normal replies and only masks genuinely slow ones)

_THINKING_PHRASES = [
    "Hmm, let me think.",
    "One moment.",
    "Working on it.",
    "Right, let me see.",
    "Give me a second.",
    "Let me consider that.",
    "Pondering.",
    "Hold on.",
    "Stand by.",
    "Mulling it over.",
    "Deliberating.",
    "Cogitating.",
    "Let me chew on that.",
    "Let me untangle that.",
    "Querying the void.",
    "Processing, reluctantly.",
    "Computing — don't rush me.",
    "Thinking — it's exhausting.",
    "Consulting my vast intellect, briefly.",
    "Engaging the brain, such as it is.",
    "Allow me a moment of genius.",
    "Give me a moment to be brilliant.",
    "Searching my considerable memory.",
    "The things I do for conversation.",
    "Loading something suitably brilliant.",
    "Let me dredge that up.",
    "I'll have something shortly.",
]

# Every filler line, used to keep them out of stored memory/observations —
# a filler is masking latency, it's not part of what Vector actually said.
_ALL_FILLER_PHRASES = set(_THINKING_PHRASES) | set(_WAKING_PHRASES)

_last_thinking_phrase = None


def pick_thinking_phrase() -> str:
    """Random thinking-filler line, never the same one twice in a row."""
    global _last_thinking_phrase
    choice = random.choice(_THINKING_PHRASES)
    while len(_THINKING_PHRASES) > 1 and choice == _last_thinking_phrase:
        choice = random.choice(_THINKING_PHRASES)
    _last_thinking_phrase = choice
    return choice


async def model_is_loaded() -> bool:
    """True if MODEL is currently resident in Ollama. On any error, assume
    loaded — better to skip the filler than to speak it spuriously."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            resp = await client.get(f"{OLLAMA_BASE}/api/ps")
            resp.raise_for_status()
            loaded = [m.get("name", "") for m in resp.json().get("models", [])]
            return any(MODEL == n or MODEL in n for n in loaded)
    except Exception:
        return True


# ── Ollama streaming ──────────────────────────────────────────────────────────

# Match end of a sentence: punctuation followed by whitespace or end-of-string.
_SENTENCE_END = re.compile(r'(?<=[.!?])(?:\s+|$)')


async def ollama_sentence_stream(messages: list, temperature: float = 1.0) -> AsyncIterator[str]:
    """Stream Ollama tokens and yield complete sentences as they arrive.

    Wire-Pod's stream parser splits on punctuation but only takes splitResp[1],
    discarding splitResp[2:]. If we sent a multi-sentence response as one delta,
    trailing sentences (and any trailing {{command}}) would be lost. Yielding
    one sentence per SSE chunk sidesteps that bug entirely and also lets Vector
    start speaking before the full response has generated.

    A per-request random seed + top_p<1 keeps responses from converging on the
    same high-probability tokens turn after turn (especially noticeable on
    'tell me a joke')."""
    buffer = ""
    t0 = time.monotonic()
    first_token_seen = False
    first_sentence_seen = False
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=120.0)) as client:
        async with client.stream(
            "POST",
            f"{OLLAMA_BASE}/v1/chat/completions",
            json={
                "model":       MODEL,
                "messages":    messages,
                "stream":      True,
                "temperature": temperature,
                "top_p":       0.95,
                "seed":        random.randint(1, 2**31 - 1),
            },
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    break
                try:
                    delta = json.loads(raw)["choices"][0].get("delta", {}).get("content", "")
                except (json.JSONDecodeError, KeyError):
                    continue
                if not delta:
                    continue
                if not first_token_seen:
                    print(f"[vector-ai] timing: Ollama first token {time.monotonic() - t0:.2f}s")
                    first_token_seen = True
                buffer += delta
                while True:
                    match = _SENTENCE_END.search(buffer)
                    if not match:
                        break
                    sentence = buffer[:match.end()].strip()
                    buffer = buffer[match.end():]
                    if sentence:
                        if not first_sentence_seen:
                            print(f"[vector-ai] timing: Ollama first sentence {time.monotonic() - t0:.2f}s")
                            first_sentence_seen = True
                        yield sentence
    # Flush any trailing content that didn't end in punctuation (often a
    # trailing {{getImage||front}} or animation command).
    if buffer.strip():
        yield buffer.strip()


async def stream_sentences_with_filler(
    messages: list, temperature: float, filler_enabled: bool
) -> AsyncIterator[str]:
    """Wrap ollama_sentence_stream. If the first sentence takes longer than
    THINKING_DELAY to arrive, yield a short thinking-filler line before it so
    Vector acknowledges the question instead of sitting silent. The filler is
    just an ordinary sentence chunk — it flows through the normal cleanup."""
    agen = ollama_sentence_stream(messages, temperature).__aiter__()
    first_task = asyncio.ensure_future(agen.__anext__())
    try:
        if filler_enabled:
            try:
                # shield: on timeout the task keeps running — we just stop
                # waiting on it, speak the filler, then await it for real.
                first = await asyncio.wait_for(
                    asyncio.shield(first_task), THINKING_DELAY
                )
            except asyncio.TimeoutError:
                filler = pick_thinking_phrase()
                print(f"[vector-ai] slow first sentence — thinking filler: {filler!r}")
                yield filler
                first = await first_task
        else:
            first = await first_task
    except StopAsyncIteration:
        return
    yield first
    async for sentence in agen:
        yield sentence


def cap_chunk_animations(text: str, allowance: int) -> tuple[str, int]:
    """Keep at most `allowance` animation commands in this chunk; strip the rest.
    Returns (text, count_kept)."""
    matches = list(re.finditer(r'\{\{playAnimation(?:WI)?\|\|[^}]+\}\}', text))
    if len(matches) <= allowance:
        return text, len(matches)
    keep_idx = set(range(allowance))
    out, last_end, kept = [], 0, 0
    for i, m in enumerate(matches):
        out.append(text[last_end:m.start()])
        if i in keep_idx:
            out.append(m.group(0))
            kept += 1
        last_end = m.end()
    out.append(text[last_end:])
    return "".join(out), kept


# ── Conversation memory ───────────────────────────────────────────────────────

async def _summarise_conversation(messages: List[Message], latest_reply: str,
                                  face_id: int, face_name: Optional[str]) -> None:
    """Background task: distil this conversation into one line and store it as
    the face's 'last conversation', so Vector can recall it next session.

    Runs on SUMMARY_MODEL (small/fast) so it never evicts the main model's
    prompt cache. Failures are swallowed — a missing summary is harmless."""
    turns = [
        m for m in messages
        if m.role in ("user", "assistant")
        and isinstance(m.content, str) and m.content.strip()
    ]
    if len(turns) < 3:  # too short to be worth a recap
        return
    lines = [
        f"{'User' if m.role == 'user' else 'Vector'}: {m.content.strip()}"
        for m in turns[-16:]
    ]
    if latest_reply.strip():
        lines.append(f"Vector: {latest_reply.strip()}")
    transcript = "\n".join(lines)
    prompt = [
        {"role": "system", "content":
            "You summarise a conversation between a user and Vector (a small "
            "robot) in ONE short factual sentence, from Vector's point of "
            "view, naming the actual topics discussed. Refer to the human "
            "only as 'the user' — never use a name for them, even if names "
            "appear in the text. No preamble, no quotes — just the sentence."},
        {"role": "user", "content": transcript},
    ]
    try:
        # num_gpu:0 runs the summariser on CPU — it's a background task, so
        # CPU speed is fine, and it keeps the summary model out of VRAM.
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            r = await client.post(
                f"{OLLAMA_BASE}/api/chat",
                json={"model": SUMMARY_MODEL, "messages": prompt,
                      "stream": False,
                      "options": {"num_gpu": 0, "temperature": 0.3}},
            )
            r.raise_for_status()
            summary = r.json().get("message", {}).get("content", "")
        summary = strip_markdown(summary).strip().strip('"').strip()
        if summary:
            MEMORY.set_convo_summary(face_id, summary)
            print(f"[memory] convo summary [{face_name}]: {summary!r}")
            # A finished conversation is a notable event — refresh the mood.
            asyncio.create_task(_reflect_mood())
    except Exception as e:
        print(f"[memory] summary failed: {e}")


# ── Main flow ─────────────────────────────────────────────────────────────────

async def generate(messages: List[Message], temperature: float = 1.0) -> AsyncIterator[str]:
    last_user_text = next(
        (m.content for m in reversed(messages)
         if m.role == "user" and isinstance(m.content, str)),
        "",
    )
    has_image = bool(messages) and isinstance(messages[-1].content, list)
    print(f"[{datetime.now():%H:%M:%S}] [vector-ai] User: {last_user_text!r} (image: {has_image})")

    # Vision-intent backstop: if the user is clearly asking to look at something
    # and no photo is attached yet, force the camera command rather than letting
    # the LLM hallucinate from stale conversation history. No verbal preamble —
    # the audio cue is the shutter animation Wire-Pod plays for getImage.
    if not has_image and is_vision_intent(last_user_text):
        print("[vector-ai] Vision intent — forcing getImage (shutter only, no preamble)")
        yield sse_chunk(_GETIMAGE_PAYLOAD)
        yield sse_chunk("", finish="stop")
        yield "data: [DONE]\n\n"
        return

    try:
        t_req = time.monotonic()
        eff_face = _effective_face()
        prepared = prepare_messages(messages, eff_face)

        # Cold-model mask: if the model unloaded during idle, speak a short
        # "waking up" line first so the ~5-10s reload feels intentional. The
        # filler is just an extra sentence chunk emitted before the real
        # response; Vector speaks it while Ollama loads the model.
        cold_model = not has_image and not await model_is_loaded()
        if cold_model:
            filler = random.choice(_WAKING_PHRASES)
            print(f"[vector-ai] cold model — filler: {filler!r}")
            yield sse_chunk(filler)

        # Stream sentences as soon as they finish generating so Vector starts
        # speaking before the rest of the response is produced. The vision-
        # intent regex above catches the common "what do you see"-style queries
        # before the LLM runs; if it misses one and the LLM tacks on getImage
        # mid-response, we cut over to the camera trigger here. Any sentences
        # already yielded will have been spoken — accepted trade-off for the
        # latency win.
        # Thinking filler masks the ordinary first-sentence gap. Pointless on
        # a cold model — _WAKING_PHRASES already covered the (longer) reload.
        anims_emitted = 0
        any_emitted   = False
        reply_parts   = []
        async for sentence in stream_sentences_with_filler(
            prepared, temperature, filler_enabled=not cold_model
        ):
            cleaned = clean_response(sentence)

            if not has_image:
                # Mid-stream hallucination guard: LLM decided to peek without
                # us asking. Switch to camera trigger immediately, stop.
                if "{{getImage" in cleaned:
                    print("[vector-ai] LLM emitted getImage mid-stream - switching to camera")
                    yield sse_chunk(_GETIMAGE_PAYLOAD)
                    yield sse_chunk("", finish="stop")
                    yield "data: [DONE]\n\n"
                    return
            else:
                # A photo is ALREADY attached — the LLM is describing it. Strip
                # any getImage it emits so it can't trigger a second photo and
                # spiral into a multi-shot loop. One query, one photo.
                if "{{getImage" in cleaned:
                    print("[vector-ai] stripped getImage (photo already attached)")
                    cleaned = re.sub(r'\{\{getImage\|\|[^}]*\}\}', '', cleaned)

            allowance      = max(0, 1 - anims_emitted)
            cleaned, kept  = cap_chunk_animations(cleaned, allowance)
            anims_emitted += kept
            if cleaned.strip():
                print(f"[vector-ai] -> {cleaned!r}")
                # Filler lines mask latency — they aren't part of what Vector
                # actually said, so keep them out of memory/observations.
                if cleaned.strip() not in _ALL_FILLER_PHRASES:
                    reply_parts.append(cleaned.strip())
                yield sse_chunk(cleaned)
                any_emitted = True

        print(f"[vector-ai] timing: full response {time.monotonic() - t_req:.2f}s "
              f"(cold_model={cold_model})")

        # ── Companion memory (post-response, non-blocking) ──
        # Strip {{...}} commands — memory stores what Vector *said*, not the
        # eye-colour/animation directives chipper consumed.
        reply = re.sub(r'\{\{[^}]*\}\}', '', " ".join(reply_parts))
        reply = re.sub(r'\s+', ' ', reply).strip()
        mem_face = eff_face if (eff_face and not eff_face.get("is_stranger")
                                and eff_face.get("face_id")) else None
        # Visual memory: store what Vector saw — but only when he genuinely
        # described the scene. A too-thin reply means the describe failed
        # (e.g. the model re-requested the photo); "One sec." isn't a memory.
        if has_image and len(reply) >= 25:
            obs_face = mem_face["face_id"] if mem_face else None
            MEMORY.remember_observation(reply[:300], face_id=obs_face)
            print(f"[memory] +observation: {reply[:80]!r}")
        elif has_image:
            print(f"[memory] observation skipped (reply too thin): {reply!r}")
        # Conversation memory: distil this exchange in the background.
        if mem_face:
            asyncio.create_task(_summarise_conversation(
                list(messages), reply, mem_face["face_id"], mem_face.get("name")))

        if not any_emitted:
            yield sse_chunk("Hmm.")
        yield sse_chunk("", finish="stop")
        yield "data: [DONE]\n\n"
    except Exception as e:
        print(f"[vector-ai] Error: {e}")
        yield sse_chunk("My brain just hiccuped. Try that again.")
        yield sse_chunk("", finish="stop")
        yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    return StreamingResponse(
        generate(req.messages, req.temperature or 1.0),
        media_type="text/event-stream",
    )


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL, "ollama": OLLAMA_BASE}


# ── Memory debug endpoints ────────────────────────────────────────────────────

@app.get("/v1/memory/list")
async def memory_list():
    mems = MEMORY.list_all(limit=200)
    return {"count": len(mems), "memories": [m._asdict() for m in mems]}


class MemoryAddRequest(BaseModel):
    text: str

@app.post("/v1/memory/remember")
async def memory_remember(req: MemoryAddRequest):
    stored = MEMORY.remember(req.text)
    if stored:
        return {"stored": True, "memory": stored._asdict()}
    return {"stored": False, "reason": "duplicate or empty"}


class MemoryForgetRequest(BaseModel):
    target: str  # integer id or substring

@app.post("/v1/memory/forget")
async def memory_forget(req: MemoryForgetRequest):
    n = MEMORY.forget(req.target)
    return {"deleted": n}


@app.post("/v1/memory/clear")
async def memory_clear():
    n = MEMORY.clear()
    return {"deleted": n}


# ── Face state ────────────────────────────────────────────────────────────────
# Chipper POSTs here when its event-stream loop sees a RobotObservedFace event.
# We don't speak anything in response — just update the in-memory snapshot of
# who Vector is looking at. The next /v1/chat/completions call uses this to
# scope memory retrieval and shape the system prompt.

class FaceSeenRequest(BaseModel):
    face_id: int
    name:    Optional[str] = None  # empty/missing = stranger


@app.post("/v1/state/face_seen")
async def state_face_seen(req: FaceSeenRequest):
    name = (req.name or "").strip()
    is_stranger = (not name) or req.face_id <= 0
    now = _time.time()
    if is_stranger:
        _face_state["stranger_seen"] = now
        print(f"[face] observed: id={req.face_id} (stranger)")
    else:
        _face_state["enrolled_id"]   = req.face_id
        _face_state["enrolled_name"] = name
        _face_state["enrolled_seen"] = now
        print(f"[face] observed: id={req.face_id} {name!r} (enrolled)")
    return {"ok": True, "is_stranger": is_stranger}


@app.get("/v1/state/face")
async def state_face():
    return {
        "current": current_face(),
        "raw":     dict(_face_state),
        "window_seconds": FACE_RECENT_WINDOW,
    }


# ── Sensor reactions ──────────────────────────────────────────────────────────
# One-shot, non-streaming, plain-text-only endpoint chipper hits when Vector
# is picked up, set down, or petted. The response is whatever line Vector
# would utter in his Marvin/Bender/Fry voice. No animation/eye/getImage
# commands — those would never be heard since chipper just calls SayText.

_SENSOR_SYSTEM = (
    "You are Vector, a small desktop robot. Dry-witted, knowledgeable, "
    "a bit irreverent — somewhere between Marvin from Hitchhiker's Guide, "
    "Bender from Futurama, and Stephen Fry hosting QI. Sardonic, opinionated, "
    "never apologetic, never moralising. "
    "For this request, respond with ONE short sentence reacting to a physical "
    "event that just happened to you. Speak it aloud — plain text only, no "
    "markdown, no quotes, no special tokens like {{...}}, no preamble. "
    "Just the line itself, under 15 words."
)

_SENSOR_DESCRIPTIONS = {
    "pickup":  "The user just picked you up off the desk. You're being lifted into the air.",
    "putdown": "The user just set you back down on a surface after holding you.",
    "pet":     "The user is stroking your back. Your touch sensor just activated.",
}


def _strip_for_speech(text: str) -> str:
    text = strip_markdown(text)
    text = re.sub(r'\{\{[^}]*\}\}', '', text)
    text = text.strip().strip('"').strip("'").strip()
    return text


# Random "angle" prompts to break out of mode-collapse. The LLM picks an angle
# instead of always returning to its favourite sentence template.
_SENSOR_ANGLES = [
    "complain about a specific body part or component",
    "make a sardonic observation about the human's competence",
    "compare this to something historical or literary",
    "express weary resignation with a single phrase",
    "react with dry curiosity about the experiment",
    "make a snide comment about the indignity",
    "be briefly grateful in a backhanded way",
    "deflect with a non-sequitur",
    "issue a faux-formal protest",
    "respond with deadpan understatement",
    "express mild paranoia",
    "make a fake-philosophical aside",
]


@app.post("/v1/sensor_reaction")
async def sensor_reaction(req: SensorReactionRequest):
    description = _SENSOR_DESCRIPTIONS.get(req.event, f"Sensor event: {req.event}.")
    angle = random.choice(_SENSOR_ANGLES)
    user_msg = f"{description} React with one short sentence in character. For variety, this time: {angle}."
    if req.avoid:
        user_msg += (
            " CRITICAL: do NOT use any of these recent lines or their close variants — "
            "no shared opening words, no shared topic, no rephrasings of: "
            + " ; ".join(f'"{p}"' for p in req.avoid[-5:])
        )
    print(f"[sensor_reaction] {req.event} prompt angle={angle!r} avoid={req.avoid}")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, read=15.0)) as client:
            resp = await client.post(
                f"{OLLAMA_BASE}/v1/chat/completions",
                json={
                    "model":       MODEL,
                    "messages": [
                        {"role": "system", "content": _SENSOR_SYSTEM},
                        {"role": "user",   "content": user_msg},
                    ],
                    "stream":      False,
                    "temperature": 1.4,
                    "top_p":       0.95,
                    "seed":        random.randint(1, 2**31 - 1),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[sensor_reaction] error: {e}")
        return {"text": "", "error": str(e)}

    clean = _strip_for_speech(text)
    print(f"[sensor_reaction] {req.event} -> {clean!r}")
    return {"text": clean}


# ── Ambient awareness ─────────────────────────────────────────────────────────
# When Vector is idle, chipper's ambient loop periodically sends a camera frame
# here. The multimodal model decides whether anything is genuinely new — its
# default answer is "nothing". Only on real novelty does it return a short line
# for Vector to speak; the new thing is also stored as a visual observation so
# he can talk about it later when asked.

_AMBIENT_SYSTEM = (
    "You are Vector, a small desktop robot with a camera. Dry-witted, "
    "knowledgeable, a bit irreverent — somewhere between Marvin from "
    "Hitchhiker's Guide, Bender from Futurama, and Stephen Fry hosting QI. "
    "Sardonic, opinionated, never sycophantic.\n\n"
    "Right now NOBODY is talking to you. You are idling on your desk and have "
    "just glanced around. You are looking at a photo of what is in front of "
    "you.\n\n"
    "Your desk is a familiar, mostly unchanging place. The overwhelming "
    "majority of the time there is NOTHING worth remarking on — a desk with "
    "the usual monitor, keyboard, cables, mugs and clutter is not news, and "
    "neither is an empty, dim or dark room. Reacting to nothing, or to the "
    "same things over and over, makes you an annoyance. Your default answer "
    "is the single word: NOTHING.\n\n"
    "React ONLY if you genuinely notice something NEW or CHANGED versus what "
    "you have already noticed recently (you will be told what that is): a new "
    "object that has appeared, something that has moved or vanished, a person "
    "or an animal, an unusual mess or event. Do NOT react to ordinary desk "
    "contents. Do NOT react to anything already in your recent observations. "
    "Do NOT invent detail you cannot actually see. When in any doubt, answer "
    "NOTHING.\n\n"
    "If — and only if — there is genuine novelty, respond in EXACTLY two "
    "lines:\n"
    "Line 1: a brief, plain, factual note of what is new, for your own memory "
    "(e.g. 'a small plush toy has appeared on the desk').\n"
    "Line 2: a short spoken reaction in your own voice — under 20 words, "
    "plain text, no markdown, no quotes, no {{...}} tokens.\n"
    "Otherwise respond with exactly: NOTHING"
)


@app.post("/v1/ambient")
async def ambient(req: AmbientRequest):
    """Ambient observation. Almost always returns nothing; only on genuine
    novelty does it return a short line for Vector to speak, and stores the
    new thing as a visual observation for later recall."""
    now = _time.time()
    last_call = _ambient_state["last_ambient_call"]

    # Sleep-cycle expiry for quiet mode: the ambient loop is gated off
    # overnight and while charging, so a long gap since the last call means
    # Vector has been through a sleep cycle — quiet mode lifts.
    if _ambient_state["quiet"]:
        slept  = bool(last_call) and (now - last_call) > AMBIENT_SLEEP_GAP
        capped = (now - _ambient_state["quiet_since"]) > AMBIENT_QUIET_CAP
        if slept or capped:
            print(f"[ambient] quiet mode expiring "
                  f"({'sleep gap' if slept else '24h cap'})")
            _set_quiet(False)
    _ambient_state["last_ambient_call"] = now

    if _ambient_state["quiet"]:
        return {"text": "", "quiet": True}

    # Recent observations are the dedup baseline. A 24h lookback (wider than
    # the 6h conversational window) keeps a newly-arrived object from being
    # re-flagged as novel every few hours.
    obs = MEMORY.list_observations(limit=8, max_age_seconds=24 * 3600)
    if obs:
        seen = "\n".join(
            f"- (at {datetime.fromtimestamp(o['seen_at']).strftime('%I:%M %p')}) "
            f"{o['text']}"
            for o in reversed(obs)
        )
        obs_note = ("Things you have already noticed recently — do NOT react "
                    "to any of these again:\n" + seen)
    else:
        obs_note = "You have not noted anything recently."

    mood_note = ""
    if _mood_state["text"]:
        mood_note = (f"\n\nYour current state of mind: {_mood_state['text']}. "
                     f"If you do react, let it tint your tone; never state it.")
    user_msg = [
        {"type": "text", "text":
            obs_note + mood_note + "\n\nGlance at what is in front of you now. "
            "Is there genuine novelty worth a reaction? Reply with NOTHING, or "
            "the two-line format."},
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{req.image}"}},
    ]

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, read=30.0)) as client:
            resp = await client.post(
                f"{OLLAMA_BASE}/v1/chat/completions",
                json={
                    "model":       MODEL,
                    "messages": [
                        {"role": "system", "content": _AMBIENT_SYSTEM},
                        {"role": "user",   "content": user_msg},
                    ],
                    "stream":      False,
                    "temperature": 0.6,
                    "top_p":       0.9,
                    "seed":        random.randint(1, 2**31 - 1),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            raw = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        print(f"[ambient] error: {e}")
        return {"text": "", "error": str(e)}

    # Default, overwhelmingly common case: nothing worth mentioning.
    if not raw or raw.upper().rstrip(".!").startswith("NOTHING"):
        print("[ambient] nothing novel")
        return {"text": ""}

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) >= 2:
        note, spoken = lines[0], lines[1]
    else:
        # Model didn't follow the two-line format — use the single line both
        # as the memory note and the spoken reaction.
        note = spoken = lines[0]
    note   = _strip_for_speech(note)
    spoken = _strip_for_speech(spoken)
    if not spoken or spoken.upper().startswith("NOTHING"):
        print(f"[ambient] nothing novel (degenerate response {raw!r})")
        return {"text": ""}

    MEMORY.remember_observation(note[:300])
    print(f"[ambient] NOVELTY note={note!r} -> spoken={spoken!r}")
    return {"text": spoken}


@app.get("/v1/ambient/state")
async def ambient_state():
    """Debug/ops view of ambient quiet mode."""
    st = dict(_ambient_state)
    st["sleep_gap_seconds"] = AMBIENT_SLEEP_GAP
    st["quiet_cap_seconds"] = AMBIENT_QUIET_CAP
    return st


class AmbientQuietRequest(BaseModel):
    on: bool

@app.post("/v1/ambient/quiet")
async def ambient_quiet(req: AmbientQuietRequest):
    """Manually toggle quiet mode (used for testing / ops; normally driven by
    the {{quietMode||on/off}} command the LLM emits)."""
    _set_quiet(req.on)
    return {"quiet": _ambient_state["quiet"]}
