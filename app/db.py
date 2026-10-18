"""SQLite storage: registered users, ticker lists, seen real-estate listings, key-value state."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id     INTEGER PRIMARY KEY,
    profile     TEXT NOT NULL,
    tg_name     TEXT,
    registered  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
    key         TEXT PRIMARY KEY,      -- "redfin:<listing id>" or "addr:<address>|<zip>"
    url         TEXT NOT NULL,
    address     TEXT NOT NULL,
    price       INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id  TEXT PRIMARY KEY,
    processed   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickers (
    profile     TEXT NOT NULL,
    list_name   TEXT NOT NULL,         -- portfolio | watch
    symbol      TEXT NOT NULL,
    added       TEXT NOT NULL,
    PRIMARY KEY (profile, list_name, symbol)
);
CREATE TABLE IF NOT EXISTS re_events (
    id          TEXT PRIMARY KEY,      -- listing key + what happened, e.g. "redfin:123:price_change:1675000"
    created     TEXT NOT NULL,
    payload     TEXT NOT NULL          -- JSON: caption / text, optional photo URL
);
CREATE TABLE IF NOT EXISTS deliveries (
    profile     TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    delivered   TEXT NOT NULL,
    PRIMARY KEY (profile, event_id)
);
CREATE TABLE IF NOT EXISTS biorxiv_history (
    profile     TEXT NOT NULL,
    doi         TEXT NOT NULL,
    sent        TEXT NOT NULL,
    PRIMARY KEY (profile, doi)
);
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    started     REAL NOT NULL,         -- unix time
    ended       REAL,                  -- NULL while running (or if the process died mid-run)
    ok          INTEGER,
    manual      INTEGER NOT NULL DEFAULT 0,
    summary     TEXT NOT NULL DEFAULT '',   -- proof of work, e.g. "3 new emails · 2 message(s) sent"
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS job_runs_job ON job_runs(job, started);
CREATE TABLE IF NOT EXISTS kv (
    k           TEXT PRIMARY KEY,
    v           TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    # ---- users -------------------------------------------------------------
    def register_user(self, chat_id: int, profile: str, tg_name: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self.tx() as c:
            c.execute(
                "INSERT INTO users(chat_id, profile, tg_name, registered) VALUES (?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET profile=excluded.profile, tg_name=excluded.tg_name",
                (chat_id, profile, tg_name, now),
            )

    def profile_for(self, chat_id: int) -> str | None:
        row = self._conn.execute("SELECT profile FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return row["profile"] if row else None

    def chat_ids_for(self, profiles: list[str]) -> list[int]:
        if not profiles:
            return []
        q = ",".join("?" * len(profiles))
        rows = self._conn.execute(f"SELECT chat_id FROM users WHERE profile IN ({q})", profiles)
        return [r["chat_id"] for r in rows]

    # ---- listings ----------------------------------------------------------
    def get_listing(self, key: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM listings WHERE key=?", (key,)).fetchone()

    def upsert_listing(self, key: str, url: str, address: str, price: int, kind: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self.tx() as c:
            c.execute(
                "INSERT INTO listings(key, url, address, price, kind, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET url=excluded.url, "
                "price=excluded.price, kind=excluded.kind, last_seen=excluded.last_seen",
                (key, url, address, price, kind, now, now),
            )

    def update_listing_url(self, key: str, url: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE listings SET url=? WHERE key=?", (url, key))

    # ---- processed emails / key-value ----------------------------------------
    def processed_email_ids(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT message_id FROM processed_emails")}

    def mark_emails_processed(self, ids: list[str]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.tx() as c:
            c.executemany("INSERT OR IGNORE INTO processed_emails(message_id, processed) VALUES (?,?)",
                          [(i, now) for i in ids])

    def get_kv(self, k: str) -> str | None:
        row = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    # ---- ticker lists ----------------------------------------------------------
    def tickers(self, profile: str, list_name: str) -> list[str]:
        rows = self._conn.execute("SELECT symbol FROM tickers WHERE profile=? AND list_name=? ORDER BY added, symbol",
                                  (profile, list_name))
        return [r[0] for r in rows]

    def add_tickers(self, profile: str, list_name: str, symbols: list[str]) -> list[str]:
        """Insert symbols; returns the ones that were newly added."""
        now = datetime.now(UTC).isoformat()
        added = []
        with self.tx() as c:
            for s in symbols:
                cur = c.execute("INSERT OR IGNORE INTO tickers(profile, list_name, symbol, added) VALUES (?,?,?,?)",
                                (profile, list_name, s, now))
                if cur.rowcount:
                    added.append(s)
        return added

    def remove_tickers(self, profile: str, list_name: str, symbols: list[str]) -> list[str]:
        removed = []
        with self.tx() as c:
            for s in symbols:
                if c.execute("DELETE FROM tickers WHERE profile=? AND list_name=? AND symbol=?",
                             (profile, list_name, s)).rowcount:
                    removed.append(s)
        return removed

    def has_any_tickers(self, profile: str) -> bool:
        return self._conn.execute("SELECT 1 FROM tickers WHERE profile=? LIMIT 1", (profile,)).fetchone() is not None

    # ---- per-person delivery of real-estate events ------------------------------
    def add_event(self, event_id: str, payload: dict) -> bool:
        """Store an event once; returns True if it is new."""
        import json
        with self.tx() as c:
            return c.execute("INSERT OR IGNORE INTO re_events(id, created, payload) VALUES (?,?,?)",
                             (event_id, datetime.now(UTC).isoformat(), json.dumps(payload))).rowcount > 0

    def undelivered_events(self, profile: str, since_iso: str) -> list[tuple[str, dict]]:
        import json
        rows = self._conn.execute(
            "SELECT e.id, e.payload FROM re_events e WHERE e.created >= ? AND NOT EXISTS "
            "(SELECT 1 FROM deliveries d WHERE d.profile=? AND d.event_id=e.id) ORDER BY e.created, e.rowid",
            (since_iso, profile))
        return [(r[0], json.loads(r[1])) for r in rows]

    def mark_delivered(self, profile: str, event_id: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO deliveries(profile, event_id, delivered) VALUES (?,?,?)",
                      (profile, event_id, datetime.now(UTC).isoformat()))

    def registered_at(self, profile: str) -> str | None:
        row = self._conn.execute("SELECT min(registered) FROM users WHERE profile=?", (profile,)).fetchone()
        return row[0] if row else None

    # ---- bioRxiv (per person) -------------------------------------------------------
    def biorxiv_sent(self, profile: str) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT doi FROM biorxiv_history WHERE profile=?", (profile,))}

    def mark_biorxiv_sent(self, profile: str, dois: list[str]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.tx() as c:
            c.executemany("INSERT OR IGNORE INTO biorxiv_history(profile, doi, sent) VALUES (?,?,?)",
                          [(profile, d, now) for d in dois])

    # ---- job run ledger (app/monitoring.py) -------------------------------------------------
    def run_started(self, job: str, ts: float, manual: bool) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO job_runs(job, started, manual) VALUES (?,?,?)", (job, ts, int(manual))).lastrowid

    def run_finished(self, row: int, ts: float, ok: bool, summary: str, error: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE job_runs SET ended=?, ok=?, summary=?, error=? WHERE id=?", (ts, int(ok), summary, error, row))

    def recent_runs(self, job: str, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM job_runs WHERE job=? ORDER BY started DESC, id DESC LIMIT ?",
                                  (job, limit)).fetchall()

    def last_starts(self) -> dict[str, float]:
        return {r[0]: r[1] for r in self._conn.execute("SELECT job, max(started) FROM job_runs GROUP BY job")}

    def run_jobs(self) -> list[str]:
        return [r[0] for r in self._conn.execute("SELECT DISTINCT job FROM job_runs ORDER BY job")]

    def kv_like(self, pattern: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self._conn.execute("SELECT k, v FROM kv WHERE k LIKE ?", (pattern,))]

    def set_kv(self, k: str, v: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO kv(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
