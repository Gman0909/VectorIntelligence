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


# Sentinel face_id used when migrating pre-multi-user rows. Override at
# runtime if the primary user's enrolled face_id differs.
_LEGACY_OWNER_FACE_ID = 44
_LEGACY_OWNER_NAME    = "G"


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
            # Add columns if migrating from the pre-multi-user schema.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(memories)").fetchall()}
            if "face_id" not in cols:
                c.execute("ALTER TABLE memories ADD COLUMN face_id INTEGER")
            if "face_name" not in cols:
                c.execute("ALTER TABLE memories ADD COLUMN face_name TEXT")
            # Existing untagged rows were all about the primary user — tag
            # them so they don't get treated as household-shared.
            c.execute(
                "UPDATE memories SET face_id = ?, face_name = ? "
                "WHERE face_id IS NULL AND face_name IS NULL",
                (_LEGACY_OWNER_FACE_ID, _LEGACY_OWNER_NAME),
            )
            c.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_text_nocase
                ON memories (text COLLATE NOCASE)
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

    def clear(self) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM memories")
            return cur.rowcount
