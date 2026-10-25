"""Persistent store (SQLite, standard library only): stored responses and cooldowns.

Why it exists: an in-memory cache and cooldown vanish on every restart, so a
blocked source could be hit again immediately and the same query re-fetched
endlessly. This store keeps both across restarts, so the bridge makes as few
live requests as possible and a source's "no" keeps being respected.

What it holds: public listing data the tools already returned (the response
JSON), UTC timestamps and cooldown state. Nothing personal: no caller
identity, no token, no log of who asked what.

One SQLite file (mode 600), WAL mode, one connection behind one lock: a single
writer. Expired cooldowns are pruned at startup and on every write. The file
lives wherever ``PRICE_SCANNER_DB_PATH`` points, outside the repository.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

KINDS = ("search", "fetch")
SOURCE_WIDE = "*"  # cooldowns.target for the per-source block cooldown
WRITE_FAILURE_THRESHOLD = 3  # consecutive failed writes before the store counts as degraded

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('search', 'fetch')),
    target      TEXT NOT NULL,
    response    TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    block_state TEXT,
    PRIMARY KEY (source, kind, target)
);
CREATE TABLE IF NOT EXISTS cooldowns (
    source              TEXT NOT NULL,
    target              TEXT NOT NULL DEFAULT '*',
    reason              TEXT,
    blocked_until       TEXT NOT NULL,
    retry_after_seconds INTEGER NOT NULL,
    PRIMARY KEY (source, target)
);
"""


class StoreError(RuntimeError):
    """The store could not be opened or read."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    """UTC ISO 8601 with offset, whole seconds: the one timestamp format used everywhere."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True)
class StoredResponse:
    source: str
    kind: str
    target: str
    response: dict[str, Any]
    fetched_at: datetime
    block_state: str | None

    def age_seconds(self, now: datetime) -> int:
        return max(0, int((now - self.fetched_at).total_seconds()))


@dataclass(frozen=True)
class Cooldown:
    source: str
    target: str
    reason: str | None
    blocked_until: datetime
    retry_after_seconds: int

    def remaining_seconds(self, now: datetime) -> int:
        return max(0, int((self.blocked_until - now).total_seconds()))


class Store:
    """Opens (or creates) the database. Raises :class:`StoreError` when it cannot."""

    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._now = clock or utc_now
        self._lock = threading.Lock()
        self.write_failures = 0  # total since start
        self.consecutive_write_failures = 0
        self.last_error: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            os.chmod(self.path, 0o600)
            pruned = self._prune(self._now())
        except (OSError, sqlite3.Error) as exc:
            raise StoreError(f"cannot open the persistent store at {self.path}: {exc}") from exc
        log.info("persistent store opened: %s (WAL, %d expired cooldown(s) pruned)", self.path, pruned)

    # -- internals -------------------------------------------------------------

    def _prune(self, now: datetime) -> int:
        return self._conn.execute("DELETE FROM cooldowns WHERE blocked_until <= ?", (iso(now),)).rowcount

    def _record_write_failure(self, what: str, exc: Exception) -> None:
        self.write_failures += 1
        self.consecutive_write_failures += 1
        self.last_error = f"{what}: {type(exc).__name__}: {exc}"
        log.error("persistent store write failed (%s; %d in a row): %s: %s",
                  what, self.consecutive_write_failures, type(exc).__name__, exc)

    def _write(self, what: str, statements: list[tuple[str, tuple[Any, ...]]]) -> bool:
        """One transaction, one writer. Returns False (logged, counted) instead of raising."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for sql, params in statements:
                        self._conn.execute(sql, params)
                    self._conn.execute("COMMIT")
                except BaseException:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            except sqlite3.Error as exc:
                self._record_write_failure(what, exc)
                return False
            self.consecutive_write_failures = 0
            return True

    def _read(self, what: str, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                self.last_error = f"{what}: {type(exc).__name__}: {exc}"
                log.error("persistent store read failed (%s): %s: %s", what, type(exc).__name__, exc)
                raise StoreError(f"persistent store read failed ({what}): {type(exc).__name__}") from exc

    # -- degraded state ----------------------------------------------------------

    @property
    def degraded(self) -> bool:
        """True after ``WRITE_FAILURE_THRESHOLD`` consecutive failed writes."""
        return self.consecutive_write_failures >= WRITE_FAILURE_THRESHOLD

    def check_writable(self) -> bool:
        """Take and release the write lock without changing data. A success ends a degraded spell."""
        return self._write("writability check", [])

    # -- responses ---------------------------------------------------------------

    def get_response(self, source: str, kind: str, target: str) -> StoredResponse | None:
        rows = self._read("response", "SELECT response, fetched_at, block_state FROM responses "
                          "WHERE source = ? AND kind = ? AND target = ?", (source, kind, target))
        if not rows:
            return None
        raw, fetched_at, block_state = rows[0]
        try:
            response = json.loads(raw)
            moment = parse_iso(fetched_at)
        except (ValueError, TypeError):
            log.error("persistent store: unreadable row for %s/%s dropped from use; it will be refreshed", source, kind)
            return None
        if not isinstance(response, dict):
            return None
        return StoredResponse(source, kind, target, response, moment, block_state)

    def put_response(self, source: str, kind: str, target: str, response: dict[str, Any],
                     fetched_at: datetime, block_state: str | None) -> bool:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        return self._write("store response", [
            ("DELETE FROM cooldowns WHERE blocked_until <= ?", (iso(self._now()),)),
            ("INSERT OR REPLACE INTO responses (source, kind, target, response, fetched_at, block_state) "
             "VALUES (?, ?, ?, ?, ?, ?)", (source, kind, target, payload, iso(fetched_at), block_state)),
        ])

    # -- cooldowns ---------------------------------------------------------------

    def put_cooldown(self, source: str, reason: str | None, blocked_until: datetime, retry_after_seconds: int,
                     target: str = SOURCE_WIDE) -> bool:
        return self._write("store cooldown", [
            ("DELETE FROM cooldowns WHERE blocked_until <= ?", (iso(self._now()),)),
            ("INSERT OR REPLACE INTO cooldowns (source, target, reason, blocked_until, retry_after_seconds) "
             "VALUES (?, ?, ?, ?, ?)", (source, target, reason, iso(blocked_until), int(retry_after_seconds))),
        ])

    def active_cooldowns(self) -> list[Cooldown]:
        rows = self._read("cooldowns", "SELECT source, target, reason, blocked_until, retry_after_seconds "
                          "FROM cooldowns WHERE blocked_until > ? ORDER BY source, target", (iso(self._now()),))
        return [Cooldown(r[0], r[1], r[2], parse_iso(r[3]), int(r[4])) for r in rows]

    def prune_expired(self) -> int:
        with self._lock:
            try:
                return self._prune(self._now())
            except sqlite3.Error as exc:
                self._record_write_failure("prune", exc)
                return 0

    # -- health ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """For ``health``: the path and counts, never the contents."""
        out: dict[str, Any] = {
            "path": str(self.path), "reachable": False, "journal_mode": None,
            "responses": None, "active_cooldowns": None,
            "write_failures": self.write_failures,
            "consecutive_write_failures": self.consecutive_write_failures,
            "degraded": self.degraded, "last_error": self.last_error,
        }
        with self._lock:
            try:
                out["responses"] = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
                out["active_cooldowns"] = self._conn.execute(
                    "SELECT COUNT(*) FROM cooldowns WHERE blocked_until > ?", (iso(self._now()),)).fetchone()[0]
                out["journal_mode"] = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
                out["reachable"] = True
            except sqlite3.Error as exc:
                out["last_error"] = f"status: {type(exc).__name__}: {exc}"
        return out

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
