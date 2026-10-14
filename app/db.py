"""SQLite storage: registered users, card offers, seen real-estate listings."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id     INTEGER PRIMARY KEY,
    profile     TEXT NOT NULL,
    tg_name     TEXT,
    registered  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile     TEXT NOT NULL,
    card_id     TEXT NOT NULL,
    merchant    TEXT NOT NULL,
    description TEXT NOT NULL,
    expires     TEXT,              -- ISO date, nullable
    created     TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
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
        now = datetime.now(timezone.utc).isoformat()
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

    # ---- offers ------------------------------------------------------------
    def add_offer(self, profile: str, card_id: str, merchant: str, description: str,
                  expires: date | None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO offers(profile, card_id, merchant, description, expires, created) "
                "VALUES (?,?,?,?,?,?)",
                (profile, card_id, merchant, description, expires.isoformat() if expires else None, now),
            )
            return int(cur.lastrowid)

    def deactivate_offer(self, offer_id: int, profile: str) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE offers SET active=0 WHERE id=? AND profile=? AND active=1",
                            (offer_id, profile))
            return cur.rowcount > 0

    def active_offers(self, profile: str | None = None, today: date | None = None) -> list[sqlite3.Row]:
        today_s = (today or date.today()).isoformat()
        sql = "SELECT * FROM offers WHERE active=1 AND (expires IS NULL OR expires >= ?)"
        args: list = [today_s]
        if profile:
            sql += " AND profile=?"
            args.append(profile)
        sql += " ORDER BY expires IS NULL, expires, merchant"
        return list(self._conn.execute(sql, args))

    # ---- listings ----------------------------------------------------------
    def get_listing(self, key: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM listings WHERE key=?", (key,)).fetchone()

    def upsert_listing(self, key: str, url: str, address: str, price: int, kind: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
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
        now = datetime.now(timezone.utc).isoformat()
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
        now = datetime.now(timezone.utc).isoformat()
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

    def kv_like(self, pattern: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self._conn.execute("SELECT k, v FROM kv WHERE k LIKE ?", (pattern,))]

    def set_kv(self, k: str, v: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO kv(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
