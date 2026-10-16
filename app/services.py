"""Shared services every feature uses, plus the feature-agnostic core jobs (Gmail check, cleanup, backup).

Blocking network work runs in threads (asyncio.to_thread) so the bot stays responsive. Feature-specific
state lives in `self.state[<feature name>]`, created by the feature's on_startup hook (or lazily).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app import maintenance
from app.access import Access
from app.config import Config
from app.db import DB
from app.features.vmhealth.vmhealth import record_failure
from app.integrations import gmail, llm
from app.notify import Notifier
from app.registry import Feature, JobFn

log = logging.getLogger(__name__)
BACKUP_DIR = Path(os.environ.get("NARI_BACKUP_DIR", "/app/backups"))


class Services:
    ERROR_ALERT_EVERY = 6 * 3600  # frequent jobs (2-hourly polls) must not page you every run

    def __init__(self, cfg: Config, db: DB, notifier: Notifier | None, features: list[Feature] | None = None):
        from app.features import ALL
        self.cfg, self.db, self.n = cfg, db, notifier
        self.features: list[Feature] = ALL if features is None else features
        self.access = Access(cfg.profiles, db)
        self.llm_health = llm.ModelHealth(db.get_kv, db.set_kv)       # remembers failing models across restarts
        self.state: dict[str, Any] = {}                              # per-feature runtime state

    # ---- helpers --------------------------------------------------------------------------
    def today(self) -> date:
        return datetime.now(self.cfg.tz).date()

    def feature(self, name: str) -> Feature | None:
        return next((f for f in self.features if f.name == name), None)

    def llm(self, models_key: str, prompt: str, *, json_output: bool = False, retries: bool = False) -> str | None:
        L = self.cfg.settings["llm"]
        return llm.summarize(
            self.cfg.gemini_api_key, L.get(models_key) or L["models"], prompt,
            timeout_seconds=L.get("timeout_seconds", 45) + (45 if retries else 0), json_output=json_output,
            attempts_per_model=L.get("brief_attempts_per_model", 1) if retries else 1,
            backoff_seconds=L.get("brief_backoff_seconds", 20), health=self.llm_health)

    async def report_error(self, job: str, e: Exception) -> None:
        log.exception("Job %s failed", job)
        record_failure(self.db, job, str(e))   # feeds /status and the repeated-failure alarm
        key = f"err_alert:{job}"
        last = float(self.db.get_kv(key) or 0)
        if time.time() - last < self.ERROR_ALERT_EVERY:
            return
        self.db.set_kv(key, str(time.time()))
        await self.n.send_text([self.cfg.primary_profile],
                               f"⚠️ Job <b>{job}</b> failed: {html.escape(str(e))[:500]}\n"
                               f"<i>(further failures of this job are logged but not alerted for 6h)</i>")

    async def send_isolated(self, pid: str, text: str, markup=None) -> None:
        """Send to one person; a failure is logged + alerted but doesn't stop the loop over people."""
        try:
            await self.n.send_text([pid], text, markup)
        except Exception as e:  # noqa: BLE001
            await self.report_error(f"send to {pid}", e)

    # ---- Gmail: ONE IMAP login per check for every feature that reads mail -----------------------
    def mail_features(self, names: list[str] | None = None) -> list[Feature]:
        """Features with a mail query that are enabled and have someone to deliver to."""
        out = []
        for f in self.features:
            if not f.mail or (names is not None and f.name not in names):
                continue
            section = self.cfg.settings.get(f.extra.get("settings_key", f.name), {})
            if section.get("enabled", True) and self.access.recipients(f.name):
                out.append(f)
        return out

    def read_mail(self, features: list[Feature]) -> dict[str, list[gmail.Mail]]:
        """Blocking. One Gmail session running each feature's search; {feature name: new mails}."""
        cfg = self.cfg
        if not (cfg.gmail_address and cfg.gmail_app_password):
            raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env")
        done = self.db.processed_email_ids()
        out: dict[str, list[gmail.Mail]] = {}
        with gmail.Session(cfg.gmail_address, cfg.gmail_app_password) as g:
            for f in features:
                q = f.mail
                out[f.name] = g.search(q.criteria(cfg.settings), q.lookback_days(cfg.settings), done,
                                       q.mailboxes(cfg.settings))
        return out

    async def mail_check(self, force: bool = False, only: list[str] | None = None) -> None:
        """Scheduled Gmail check (every 2 hours). `only` limits it to some features (used by /run)."""
        feats = self.mail_features(only)
        if not feats:
            return
        try:
            mails = await asyncio.to_thread(self.read_mail, feats)
        except Exception as e:  # noqa: BLE001
            await self.report_error("gmail", e)
            return
        for f in feats:
            await f.mail.handle(self, mails.get(f.name, []), force)

    # ---- maintenance ------------------------------------------------------------------------
    async def cleanup(self, force: bool = False) -> None:
        """Nightly: retention + leftovers; VACUUM on Sundays."""
        try:
            pruned = await asyncio.to_thread(maintenance.prune, self.db)
            fixed = await asyncio.to_thread(maintenance.one_time_cleanup, self.db)
            if self.today().weekday() == 6 or force:
                await asyncio.to_thread(maintenance.vacuum, self.db)
            log.info("Cleanup: pruned %s, fixed %s", pruned, fixed)
            if force:
                await self.n.send_text([self.cfg.primary_profile], f"🧹 Cleanup: pruned {pruned}; leftovers {fixed}")
        except Exception as e:  # noqa: BLE001
            await self.report_error("cleanup", e)

    async def backup(self, force: bool = False) -> None:
        try:
            keep = self.cfg.settings.get("maintenance", {}).get("backup_keep", 8)
            path = await asyncio.to_thread(maintenance.backup, self.db, BACKUP_DIR, keep)
            log.info("Backup written: %s", path)
            if force:
                await self.n.send_text([self.cfg.primary_profile], f"💾 Backup written: {path.name}")
        except Exception as e:  # noqa: BLE001
            await self.report_error("backup", e)

    # ---- job registry -----------------------------------------------------------------------
    def core_jobs(self) -> dict[str, tuple[JobFn, dict | str | None]]:
        mt = self.cfg.settings.get("maintenance", {})
        mail_sched = self.cfg.settings.get("gmail", {}).get("schedule") or \
            self.cfg.settings.get("real_estate", {}).get("alerts_schedule")
        return {"mail": (lambda s, force=False: s.mail_check(force), mail_sched),
                "cleanup": (lambda s, force=False: s.cleanup(force), mt.get("cleanup_schedule", {"hour": 3, "minute": 0})),
                "backup": (lambda s, force=False: s.backup(force),
                           mt.get("backup_schedule", {"day_of_week": "sun", "hour": 3, "minute": 30}))}

    def jobs(self) -> dict[str, tuple[JobFn, dict | str | None]]:
        """Every /run-able job: {id: (fn(services, force=...), schedule or None)}."""
        out = self.core_jobs()
        for f in self.features:
            for j in f.jobs:
                out[j.id] = (j.run, j.schedule(self.cfg.settings))
        return out

    async def run_job(self, job_id: str, force: bool = True) -> None:
        fn, _ = self.jobs()[job_id]
        await fn(self, force=force)
