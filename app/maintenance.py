"""Data retention, one-time cleanups and backups for the SQLite store.

Retention (nightly):
  processed_emails   180 days   — must exceed the longest Gmail look-back, or old mail would be re-processed
  listings           180 days since last seen
  re_events          30 days    — real-estate alerts, delivered per person (deliveries table follows)
  quarter state      keep previous, current and next quarter (5% categories, /setcats, activations)
  err_alert / llm    30 days
  ticker info cache  30 days    — refetched from Yahoo after expiry
Backups (weekly): sqlite online-backup into BACKUP_DIR, newest N kept.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.db import DB

log = logging.getLogger(__name__)

EMAIL_RETENTION_DAYS = 180
LISTING_RETENTION_DAYS = 180
SHORT_RETENTION_DAYS = 30
QUARTER_KEY_RE = re.compile(r"^(rotq|rotating|activated|rotact):(?:[^:]+:)+(\d{4})Q([1-4])$")


class TickerInfoCache(dict):
    """symbol -> (name, quote type), persisted in kv as tick:<SYMBOL> and expired by prune() after 30 days,
    so company names aren't re-fetched from Yahoo after every restart."""

    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        for k, v in db.kv_like("tick:%"):
            d = json.loads(v)
            super().__setitem__(k[5:], (d["name"], d["type"]))

    def __setitem__(self, symbol: str, value: tuple[str, str]) -> None:
        super().__setitem__(symbol, value)
        self.db.set_kv(f"tick:{symbol}", json.dumps({"name": value[0], "type": value[1], "ts": int(time.time())}))


def _quarter_index(year: int, q: int) -> int:
    return year * 4 + (q - 1)


def check_email_retention(settings: dict[str, Any]) -> None:
    lookbacks = [settings["real_estate"]["email"]["lookback_days"], settings["cards"]["email"]["lookback_days"]]
    if EMAIL_RETENTION_DAYS <= max(lookbacks):
        raise ValueError(f"processed_emails retention ({EMAIL_RETENTION_DAYS}d) must exceed the longest Gmail "
                         f"look-back ({max(lookbacks)}d), or old emails would be processed again")


def prune(db: DB, today: date | None = None, now: float | None = None) -> dict[str, int]:
    """Apply retention. Returns rows deleted per category."""
    today = today or date.today()
    now = now if now is not None else time.time()
    iso = lambda days: (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()  # noqa: E731
    cur_q = _quarter_index(today.year, (today.month - 1) // 3 + 1)
    out: dict[str, int] = {}
    with db.tx() as c:
        out["emails"] = c.execute("DELETE FROM processed_emails WHERE processed < ?",
                                  (iso(EMAIL_RETENTION_DAYS),)).rowcount
        out["listings"] = c.execute("DELETE FROM listings WHERE last_seen < ?", (iso(LISTING_RETENTION_DAYS),)).rowcount
        out["re_events"] = c.execute("DELETE FROM re_events WHERE created < ?", (iso(SHORT_RETENTION_DAYS),)).rowcount
        out["deliveries"] = c.execute("DELETE FROM deliveries WHERE event_id NOT IN (SELECT id FROM re_events)").rowcount
        stale_quarters = []
        for (k,) in c.execute("SELECT k FROM kv"):
            if (m := QUARTER_KEY_RE.match(k)) and _quarter_index(int(m.group(2)), int(m.group(3))) < cur_q - 1:
                stale_quarters.append(k)
        out["quarters"] = sum(c.execute("DELETE FROM kv WHERE k=?", (k,)).rowcount for k in stale_quarters)
        old_alerts = [k for k, v in c.execute("SELECT k, v FROM kv WHERE k LIKE 'err_alert:%'")
                      if float(v or 0) < now - SHORT_RETENTION_DAYS * 86400]
        out["err_alerts"] = sum(c.execute("DELETE FROM kv WHERE k=?", (k,)).rowcount for k in old_alerts)
        old_tick = [k for k, v in c.execute("SELECT k, v FROM kv WHERE k LIKE 'tick:%'")
                    if json.loads(v).get("ts", 0) < now - SHORT_RETENTION_DAYS * 86400]
        out["ticker_info"] = sum(c.execute("DELETE FROM kv WHERE k=?", (k,)).rowcount for k in old_tick)
        row = c.execute("SELECT v FROM kv WHERE k='llm_health'").fetchone()
        if row:
            state = json.loads(row[0])
            keep = {m: s for m, s in state.items()
                    if max(s.get("last_ok", 0), s.get("last_fail", 0), s.get("until", 0)) > now - SHORT_RETENTION_DAYS * 86400}
            out["llm_models"] = len(state) - len(keep)
            c.execute("UPDATE kv SET v=? WHERE k='llm_health'", (json.dumps(keep),))
    return out


def one_time_cleanup(db: DB) -> dict[str, int]:
    """Idempotent fixes for leftovers of earlier versions."""
    out = {}
    with db.tx() as c:
        # Listings parsed by the old LLM-only path: synthetic "addr:" keys and zipcode links.
        out["legacy_listings"] = c.execute("DELETE FROM listings WHERE key LIKE 'addr:%' AND url LIKE '%/zipcode/%'").rowcount
        out["offers_table"] = 1 if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='offers'").fetchone() else 0
        c.execute("DROP TABLE IF EXISTS offers")
    return out


def vacuum(db: DB) -> None:
    db._conn.execute("VACUUM")


def backup(db: DB, backup_dir: Path, keep: int = 8) -> Path:
    """Consistent online copy (sqlite backup API) + keep the newest `keep` files."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"nari-{datetime.now():%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(dest) as out:
        db._conn.backup(out)
    for old in sorted(backup_dir.glob("nari-*.db"))[:-keep]:
        old.unlink()
    return dest
