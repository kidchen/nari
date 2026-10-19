"""Data retention, one-time cleanups and backups for the SQLite store.

Retention (nightly):
  processed_emails   180 days   — must exceed the longest Gmail look-back, or old mail would be re-processed
  listings           180 days since last seen
  biorxiv_history    90 days    — DOIs already sent, per person
  re_events          30 days    — real-estate alerts, delivered per person (deliveries table follows)
  quarter state      keep previous, current and next quarter (5% categories, /setcats, activations)
  err_alert / llm    30 days
  statement PDFs     30 days    — data/statements/inbox (files, not DB rows)
  job_runs           30 days    — the job ledger behind /status and the job alarms
  ticker info cache  30 days    — refetched from Yahoo after expiry
Backups (weekly): sqlite online-backup into BACKUP_DIR, newest N kept.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.db import DB

log = logging.getLogger(__name__)

EMAIL_RETENTION_DAYS = 180
LISTING_RETENTION_DAYS = 180
SHORT_RETENTION_DAYS = 30
BIORXIV_RETENTION_DAYS = 90    # sent-paper DOIs; far beyond the 7-day look-back, so nothing is re-sent
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
    lookbacks = [settings["real_estate"]["email"]["lookback_days"], settings["cards"]["email"]["lookback_days"],
                 ((settings.get("expenses") or {}).get("alerts") or {}).get("lookback_days", 14)]
    if EMAIL_RETENTION_DAYS <= max(lookbacks):
        raise ValueError(f"processed_emails retention ({EMAIL_RETENTION_DAYS}d) must exceed the longest Gmail "
                         f"look-back ({max(lookbacks)}d), or old emails would be processed again")


def prune(db: DB, today: date | None = None, now: float | None = None) -> dict[str, int]:
    """Apply retention. Returns rows deleted per category."""
    today = today or date.today()
    now = now if now is not None else time.time()
    iso = lambda days: (datetime.now(UTC) - timedelta(days=days)).isoformat()  # noqa: E731
    cur_q = _quarter_index(today.year, (today.month - 1) // 3 + 1)
    out: dict[str, int] = {}
    with db.tx() as c:
        out["emails"] = c.execute("DELETE FROM processed_emails WHERE processed < ?",
                                  (iso(EMAIL_RETENTION_DAYS),)).rowcount
        out["listings"] = c.execute("DELETE FROM listings WHERE last_seen < ?", (iso(LISTING_RETENTION_DAYS),)).rowcount
        out["biorxiv"] = c.execute("DELETE FROM biorxiv_history WHERE sent < ?", (iso(BIORXIV_RETENTION_DAYS),)).rowcount
        out["re_events"] = c.execute("DELETE FROM re_events WHERE created < ?", (iso(SHORT_RETENTION_DAYS),)).rowcount
        out["statements"] = _prune_files(Path(db.path).parent / "statements", now - SHORT_RETENTION_DAYS * 86400) \
            if db.path != ":memory:" else 0
        out["job_runs"] = c.execute("DELETE FROM job_runs WHERE started < ?", (now - SHORT_RETENTION_DAYS * 86400,)).rowcount
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


def _prune_files(root: Path, cutoff: float) -> int:
    n = 0
    for f in root.rglob("*") if root.exists() else []:
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            n += 1
    return n


def one_time_cleanup(db: DB) -> dict[str, int]:
    """Idempotent fixes for leftovers of earlier versions."""
    out = {}
    with db.tx() as c:
        # Listings parsed by the old LLM-only path: synthetic "addr:" keys and zipcode links.
        out["legacy_listings"] = c.execute("DELETE FROM listings WHERE key LIKE 'addr:%' AND url LIKE '%/zipcode/%'").rowcount
        out["offers_table"] = 1 if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='offers'").fetchone() else 0
        c.execute("DROP TABLE IF EXISTS offers")
        # Pre-ledger job failure tracking (health:fail:<job>) and its old-format job alarms.
        out["legacy_job_health"] = c.execute(
            "DELETE FROM kv WHERE k LIKE 'health:fail:%' OR (k LIKE 'alarm:job:%' AND v IN ('0', '1'))").rowcount
    return out


def vacuum(db: DB) -> None:
    db._conn.execute("VACUUM")


def _age_encrypt(src: Path, recipient: str) -> Path:
    """Encrypt to <src>.age with an age public key, then remove the plaintext. The server only ever has
    the public key, so it can write backups but not read them (the private key stays with the admin)."""
    import subprocess
    dest = src.with_name(src.name + ".age")
    subprocess.run(["age", "-r", recipient, "-o", str(dest), str(src)], check=True, capture_output=True, timeout=120)
    dest.chmod(0o600)
    src.unlink()
    return dest


def _backups(backup_dir: Path) -> list[Path]:
    return sorted(list(backup_dir.glob("nari-*.db")) + list(backup_dir.glob("nari-*.db.age")), key=lambda p: p.name)


def backup(db: DB, backup_dir: Path, keep: int = 8, recipient: str | None = None) -> Path:
    """Consistent online copy (sqlite backup API), age-encrypted if `recipient` is set; keep the newest `keep`."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"nari-{datetime.now():%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(dest) as out:
        db._conn.backup(out)
    dest.chmod(0o600)
    if recipient:
        dest = _age_encrypt(dest, recipient)
    else:
        log.warning("Backup written UNENCRYPTED: set maintenance.backup_age_recipient")
    for old in _backups(backup_dir)[:-keep]:
        old.unlink()
    return dest


def encrypt_old_backups(backup_dir: Path, recipient: str | None) -> int:
    """Once a recipient is configured, encrypt any plaintext backups left from before (idempotent)."""
    if not recipient or not backup_dir.exists():
        return 0
    plain = sorted(backup_dir.glob("nari-*.db"))
    for p in plain:
        _age_encrypt(p, recipient)
    return len(plain)
