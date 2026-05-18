"""Persistent memory for Vector. SQLite-backed, one row per durable fact.

Memories are durable facts about the user (names, preferences, ongoing
projects, things they've told Vector). Each is a single short string. The
LLM decides what to store via the {{remember||...}} command emitted in
its response; service.py captures and saves them here.

All memories are injected into the system prompt on every turn so the LLM
can refer to them. For a single-user companion bot, this scales fine into
the thousands of entries before context becomes a concern.
"""
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, NamedTuple, Optional


class Memory(NamedTuple):
    id: int
    created_at: str
    face_id: Optional[int]    # None = shared / household
    face_name: Optional[str]  # denormalized so renames in firmware don't orphan rows
    text: str


# Visual observations are only ever recalled within the last few hours, so
# anything older is dead weight — pruned on each write to keep the table
# bounded. (The captured images themselves are never stored at all.)
_OBSERVATION_RETENTION = 7 * 24 * 3600  # seconds


class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self):
        with self._lock, self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    face_id    INTEGER,
                    face_name  TEXT,
                    text       TEXT NOT NULL
                )
            """)
            # Defensive: add the multi-user columns if an older single-user
            # schema is ever encountered. A fresh install already has them
            # (see CREATE TABLE above), so this is normally a no-op.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(memories)").fetchall()}
            if "face_id" not in cols:
                c.execute("ALTER TABLE memories ADD COLUMN face_id INTEGER")
            if "face_name" not in cols:
                c.execute("ALTER TABLE memories ADD COLUMN face_name TEXT")
            c.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_text_nocase
                ON memories (text COLLATE NOCASE)
            """)
            # Per-face interaction metadata: when Vector last spoke with this
            # person, how many times, and a one-line recap of their last
            # conversation. Powers temporal presence + conversation memory.
            c.execute("""
                CREATE TABLE IF NOT EXISTS face_meta (
                    face_id            INTEGER PRIMARY KEY,
                    face_name          TEXT,
                    first_seen         REAL,
                    last_seen          REAL,
                    interaction_count  INTEGER DEFAULT 0,
                    last_convo_summary TEXT,
                    last_convo_at      REAL
                )
            """)
            # Visual memory: a short description each time Vector actually
            # takes a photo. Not deduplicated (the same scene at two times is
            # two valid observations) — hence its own table, not `memories`.
            c.execute("""
                CREATE TABLE IF NOT EXISTS observations (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    seen_at REAL,
                    face_id INTEGER,
                    text    TEXT NOT NULL
                )
            """)
            # Generic key/value state — currently holds Vector's persistent
            # mood (Phase 2 continuity) so his disposition survives restarts.
            c.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    key        TEXT PRIMARY KEY,
                    value      TEXT,
                    updated_at REAL
                )
            """)

    def remember(
        self,
        text: str,
        face_id: Optional[int] = None,
        face_name: Optional[str] = None,
    ) -> Optional[Memory]:
        """Store a new memory. face_id/face_name NULL means shared/household.
        Returns the row, or None if a duplicate exists."""
        text = text.strip()
        if not text:
            return None
        with self._lock, self._conn() as c:
            try:
                cur = c.execute(
                    "INSERT INTO memories (text, face_id, face_name) VALUES (?, ?, ?)",
                    (text, face_id, face_name),
                )
                mem_id = cur.lastrowid
            except sqlite3.IntegrityError:
                return None
            row = c.execute(
                "SELECT id, created_at, face_id, face_name, text "
                "FROM memories WHERE id = ?",
                (mem_id,),
            ).fetchone()
        return Memory(
            row["id"], row["created_at"], row["face_id"], row["face_name"], row["text"]
        )

    def forget(self, text_or_id: str) -> int:
        text_or_id = text_or_id.strip()
        if not text_or_id:
            return 0
        with self._lock, self._conn() as c:
            if text_or_id.isdigit():
                cur = c.execute("DELETE FROM memories WHERE id = ?", (int(text_or_id),))
            else:
                cur = c.execute(
                    "DELETE FROM memories WHERE text LIKE ? COLLATE NOCASE",
                    (f"%{text_or_id}%",),
                )
            return cur.rowcount

    def _rows_to_memories(self, rows) -> List[Memory]:
        return [
            Memory(r["id"], r["created_at"], r["face_id"], r["face_name"], r["text"])
            for r in rows
        ]

    def list_all(self, limit: int = 200) -> List[Memory]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, created_at, face_id, face_name, text "
                "FROM memories ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return self._rows_to_memories(rows)

    def list_for_face(self, face_id: int, limit: int = 100) -> List[Memory]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, created_at, face_id, face_name, text "
                "FROM memories WHERE face_id = ? ORDER BY id DESC LIMIT ?",
                (face_id, limit),
            ).fetchall()
        return self._rows_to_memories(rows)

    def list_mentions_of_name(
        self,
        name: str,
        exclude_face_id: Optional[int] = None,
        limit: int = 50,
    ) -> List[Memory]:
        """Return memories from OTHER profiles (or shared) that mention `name`
        as a whole word — used to surface cross-references like 'Sarah is G's
        wife' when Vector is looking at Sarah, even though that fact is tagged
        to G's profile."""
        name = name.strip()
        if not name:
            return []
        pattern = re.compile(r'\b' + re.escape(name) + r'\b', re.IGNORECASE)
        with self._lock, self._conn() as c:
            if exclude_face_id is None:
                rows = c.execute(
                    "SELECT id, created_at, face_id, face_name, text "
                    "FROM memories WHERE text LIKE ? COLLATE NOCASE "
                    "ORDER BY id DESC LIMIT ?",
                    (f"%{name}%", limit * 4),  # over-fetch, filter below
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, created_at, face_id, face_name, text "
                    "FROM memories "
                    "WHERE text LIKE ? COLLATE NOCASE "
                    "AND (face_id IS NULL OR face_id != ?) "
                    "ORDER BY id DESC LIMIT ?",
                    (f"%{name}%", exclude_face_id, limit * 4),
                ).fetchall()
        # Word-boundary filter — drops 'Sarahville' / partial matches.
        out = [
            Memory(r["id"], r["created_at"], r["face_id"], r["face_name"], r["text"])
            for r in rows if pattern.search(r["text"])
        ]
        return out[:limit]

    def list_shared(self, limit: int = 100) -> List[Memory]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, created_at, face_id, face_name, text "
                "FROM memories WHERE face_id IS NULL ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return self._rows_to_memories(rows)

    def distinct_faces(self) -> List[tuple]:
        """Returns [(face_id, face_name), ...] for every face that has at
        least one memory. Used to identify the primary user when no face is
        being actively detected — a single enrolled profile means a
        single-user setup, so 'no face seen' can safely default to them."""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT face_id, face_name FROM memories "
                "WHERE face_id IS NOT NULL ORDER BY face_id"
            ).fetchall()
        return [(r["face_id"], r["face_name"]) for r in rows]

    # ── Per-face interaction metadata ─────────────────────────────────────────

    def touch_face(self, face_id: int, face_name: Optional[str]) -> Optional[dict]:
        """Record that Vector is interacting with this face right now.

        Returns the face's metadata as it was BEFORE this call (prior
        last_seen, interaction_count, last conversation summary) so the caller
        can build temporal context — or None if this is the first ever
        interaction with this face."""
        if face_id is None or face_id <= 0:
            return None
        now = datetime.now().timestamp()
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT face_id, face_name, first_seen, last_seen, "
                "interaction_count, last_convo_summary, last_convo_at "
                "FROM face_meta WHERE face_id = ?", (face_id,),
            ).fetchone()
            prior = dict(row) if row else None
            if row:
                c.execute(
                    "UPDATE face_meta SET face_name = ?, last_seen = ?, "
                    "interaction_count = interaction_count + 1 WHERE face_id = ?",
                    (face_name, now, face_id),
                )
            else:
                c.execute(
                    "INSERT INTO face_meta (face_id, face_name, first_seen, "
                    "last_seen, interaction_count) VALUES (?, ?, ?, ?, 1)",
                    (face_id, face_name, now, now),
                )
        return prior

    def get_face_meta(self, face_id: int) -> Optional[dict]:
        if face_id is None or face_id <= 0:
            return None
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT face_id, face_name, first_seen, last_seen, "
                "interaction_count, last_convo_summary, last_convo_at "
                "FROM face_meta WHERE face_id = ?", (face_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_convo_summary(self, face_id: int, summary: str) -> None:
        """Store a one-line recap of the most recent conversation with a face."""
        summary = (summary or "").strip()
        if face_id is None or face_id <= 0 or not summary:
            return
        now = datetime.now().timestamp()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO face_meta "
                "(face_id, first_seen, last_seen, interaction_count) "
                "VALUES (?, ?, ?, 0)", (face_id, now, now),
            )
            c.execute(
                "UPDATE face_meta SET last_convo_summary = ?, last_convo_at = ? "
                "WHERE face_id = ?", (summary, now, face_id),
            )

    # ── Visual memory ─────────────────────────────────────────────────────────

    def remember_observation(self, text: str, face_id: Optional[int] = None) -> None:
        """Store something Vector saw (a photo description). Each write also
        prunes observations older than _OBSERVATION_RETENTION, so the table
        stays bounded — they are never recalled past a few hours anyway."""
        text = (text or "").strip()
        if not text:
            return
        now = datetime.now().timestamp()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO observations (seen_at, face_id, text) VALUES (?, ?, ?)",
                (now, face_id, text),
            )
            c.execute(
                "DELETE FROM observations WHERE seen_at < ?",
                (now - _OBSERVATION_RETENTION,),
            )

    def list_observations(self, limit: int = 5, max_age_seconds: int = 21600) -> List[dict]:
        """Recent things Vector saw — defaults to the last 6 hours."""
        cutoff = datetime.now().timestamp() - max_age_seconds
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, seen_at, face_id, text FROM observations "
                "WHERE seen_at >= ? ORDER BY id DESC LIMIT ?", (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Persistent state (key/value) ──────────────────────────────────────────

    def get_state(self, key: str) -> Optional[dict]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT value, updated_at FROM state WHERE key = ?", (key,),
            ).fetchone()
        return {"value": row["value"], "updated_at": row["updated_at"]} if row else None

    def set_state(self, key: str, value: str) -> None:
        now = datetime.now().timestamp()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (key, value, now),
            )

    def latest_conversation(self) -> Optional[dict]:
        """The most recent conversation across all faces — feeds mood
        reflection (how long since Vector last spoke with someone, about what)."""
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT face_name, last_convo_summary, last_convo_at "
                "FROM face_meta WHERE last_convo_at IS NOT NULL "
                "ORDER BY last_convo_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def clear(self) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM memories")
            c.execute("DELETE FROM observations")
            c.execute("UPDATE face_meta SET last_convo_summary = NULL, last_convo_at = NULL")
            return cur.rowcount
