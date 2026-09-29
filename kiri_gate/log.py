"""Every decision, in SQLite. This is the raw material for calibration and for learning your line.
Arguments go through redact.py before they're written: secret-looking fields are stored as [REDACTED]."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Dict, FrozenSet, List, Optional

from kiri_gate.redact import redact


class DecisionLog:
    def __init__(self, path: str = "kiri.db") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute("""CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY, ts REAL, tool TEXT, cls TEXT, goal TEXT, args TEXT,
            decision TEXT, why TEXT, confidence REAL, approved INTEGER, edited INTEGER, note TEXT)""")
        self._db.commit()

    def record(self, ts: float, tool: str, cls: str, goal: str, args: Dict[str, Any], decision: str, why: str,
               confidence: Optional[float], approved: Optional[bool], edited: bool, note: str,
               sensitive: FrozenSet[str] = frozenset()) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO decisions (ts, tool, cls, goal, args, decision, why, confidence, approved, edited, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, tool, cls, goal, json.dumps(redact(args, sensitive), default=str)[:4000], decision, why, confidence,
                 None if approved is None else int(approved), int(edited), note))
            self._db.commit()

    def rows(self) -> List[Dict[str, Any]]:
        cur = self._db.execute("SELECT * FROM decisions ORDER BY id")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Newest first."""
        with self._lock:
            cur = self._db.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (int(limit),))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def labelled(self) -> List[tuple]:
        """(confidence, 1 if approved unchanged else 0) for asked decisions that had a score."""
        return [(r["confidence"], int(r["approved"] == 1 and not r["edited"])) for r in self.rows()
                if r["decision"] == "ask" and r["confidence"] is not None and r["approved"] is not None]
