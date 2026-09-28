"""Effect ledger: so a write that already happened isn't done again.

Every IRREVERSIBLE or EXTERNAL call gets a key made from the tool, its arguments and a scope (the goal, or
the MCP server). A model that re-plans after a timeout sends the same arguments, so it gets the same key,
even though the framework gave it a new call id.

States:
  CLAIMED  running right now
  DONE     finished; an identical call gets the saved result and nothing runs again
  UNKNOWN  raised an error, so it may or may not have happened (a timeout after the write went through looks
           exactly like one before it). An identical call never runs blindly: it's reconciled or asked about.

Entries expire after `window` seconds (default 24h, the same as Stripe's idempotency keys). A claim older than
`stale` seconds (default 10 minutes) counts as UNKNOWN: the process running it probably died mid-call.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any, Dict, Optional, Tuple

CLAIMED, DONE, UNKNOWN = "claimed", "done", "unknown"
KEY_ARG = "idempotency_key"
MAX_RESULT = 1_000_000  # characters of JSON; a bigger result isn't kept (the DONE state still is)


def make_key(tool: str, args: Dict[str, Any], scope: str = "") -> str:
    """Same tool + same arguments + same scope = same key. idempotency_key itself is left out."""
    clean = {k: v for k, v in args.items() if k != KEY_ARG}
    body = json.dumps([scope, tool, clean], sort_keys=True, default=str, separators=(",", ":"))
    return "kiri-" + hashlib.sha256(body.encode()).hexdigest()[:40]


class Ledger:
    def __init__(self, path: str = ":memory:", window: float = 86400.0, stale: float = 600.0) -> None:
        self.window, self.stale = window, stale
        # Autocommit, so claim() can take an IMMEDIATE lock: two processes sharing a file can't both claim.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self._lock = threading.Lock()
        self._results: Dict[str, Any] = {}  # live result objects, while this process still has them
        self._db.execute("""CREATE TABLE IF NOT EXISTS effects (
            key TEXT PRIMARY KEY, tool TEXT, args TEXT, state TEXT, result TEXT, error TEXT, ts REAL)""")

    def close(self) -> None:
        self._db.close()

    def _state(self, row) -> Optional[Tuple[str, str]]:
        """(state, error) for a (state, error, ts) row, after expiry and staleness. None if absent or expired."""
        if row is None:
            return None
        state, error, ts = row
        age = time.time() - ts
        if age > self.window:
            return None
        if state == CLAIMED and age > self.stale:
            return UNKNOWN, "an identical call started earlier and never finished (the process may have died)"
        return state, error or ""

    def get(self, key: str) -> Tuple[Optional[str], Any, str]:
        """(state or None, result, error). Expired entries count as absent."""
        with self._lock:
            row = self._db.execute("SELECT state, error, ts, result FROM effects WHERE key=?", (key,)).fetchone()
        st = self._state(row[:3] if row else None)
        if st is None:
            return None, None, ""
        state, error = st
        if key in self._results:
            return state, self._results[key], error
        try:
            return state, (json.loads(row[3]) if row[3] else None), error
        except ValueError:
            return state, row[3], error

    def claim(self, key: str, tool: str, args: Dict[str, Any]) -> bool:
        """Mark as running. False if an identical call is running or done. UNKNOWN can be claimed: that's
        the caller deciding to run it after reconciling or asking."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT state, error, ts FROM effects WHERE key=?", (key,)).fetchone()
                st = self._state(row)
                if st is not None and st[0] != UNKNOWN:
                    self._db.execute("ROLLBACK")
                    return False
                self._db.execute(
                    "INSERT OR REPLACE INTO effects (key, tool, args, state, result, error, ts) VALUES (?,?,?,?,?,?,?)",
                    (key, tool, json.dumps(args, default=str)[:4000], CLAIMED, None, None, time.time()))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._results.pop(key, None)
        return True

    def done(self, key: str, result: Any) -> None:
        self._results[key] = result
        raw = json.dumps(result, default=str)
        self._set(key, DONE, result=raw if len(raw) <= MAX_RESULT else None)

    def unknown(self, key: str, error: str) -> None:
        self._set(key, UNKNOWN, error=error[:500])

    def release(self, key: str) -> None:
        """The call definitely did not happen (it raised NotExecuted): forget it, so a retry can run."""
        with self._lock:
            self._db.execute("DELETE FROM effects WHERE key=?", (key,))
            self._results.pop(key, None)

    def _set(self, key: str, state: str, result: Optional[str] = None, error: Optional[str] = None) -> None:
        with self._lock:
            self._db.execute("UPDATE effects SET state=?, result=COALESCE(?, result), error=COALESCE(?, error), ts=? "
                             "WHERE key=?", (state, result, error, time.time(), key))
