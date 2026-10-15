"""Scheduled jobs. Blocking network work runs in threads so the bot stays responsive."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app import maintenance
from app.access import Access
from app.config import Config
from app.db import DB
from app.modules import (brief, cards, gmail, llm, mortgage, placeholder, realestate, redfin_email, rotating,
                         vmhealth)
from app.notify import Notifier, buttons

log = logging.getLogger(__name__)
BACKUP_DIR = Path(os.environ.get("NARI_BACKUP_DIR", "/app/backups"))


class Jobs:
    def __init__(self, cfg: Config, db: DB, notifier: Notifier):
        self.cfg, self.db, self.n = cfg, db, notifier
        self._name_cache = maintenance.TickerInfoCache(db)  # symbol -> (name, quote type), kept 30 days in the DB
        self.access = Access(cfg.profiles, db)
        self.llm_health = llm.ModelHealth(db.get_kv, db.set_kv)       # remembers failing models across restarts
        self._brief_cache: dict[str, tuple[float, tuple, str]] = {}  # pid -> (ts, lists, text)

    def _llm(self, models_key: str, prompt: str, *, json_output: bool = False, retries: bool = False) -> str | None:
        L = self.cfg.settings["llm"]
        return llm.summarize(
            self.cfg.gemini_api_key, L.get(models_key) or L["models"], prompt,
            timeout_seconds=L.get("timeout_seconds", 45) + (45 if retries else 0), json_output=json_output,
            attempts_per_model=L.get("brief_attempts_per_model", 1) if retries else 1,
            backoff_seconds=L.get("brief_backoff_seconds", 20), health=self.llm_health)

    def _today(self) -> date:
        return datetime.now(self.cfg.tz).date()

    ERROR_ALERT_EVERY = 6 * 3600  # frequent jobs (2-hourly polls) must not page you every run

    async def _report_error(self, job: str, e: Exception) -> None:
        log.exception("Job %s failed", job)
        vmhealth.record_failure(self.db, job, str(e))   # feeds /health and the repeated-failure alarm
        key = f"err_alert:{job}"
        last = float(self.db.get_kv(key) or 0)
        if time.time() - last < self.ERROR_ALERT_EVERY:
            return
        self.db.set_kv(key, str(time.time()))
        await self.n.send_text([self.cfg.primary_profile],
                               f"⚠️ Job <b>{job}</b> failed: {html.escape(str(e))[:500]}\n"
                               f"<i>(further failures of this job are logged but not alerted for 6h)</i>")

    # Module A
    async def placeholder(self, force: bool = False) -> None:
        """Monthly placeholder, per person: defaults from settings.placeholder, overridden by
        profiles.<id>.placeholder (label, amount)."""
        today = self._today()
        for pid in self.access.recipients("placeholder"):
            c = {**self.cfg.settings["placeholder"], **(self.cfg.profiles[pid].get("placeholder") or {})}
            try:
                png = await asyncio.to_thread(placeholder.render, today.replace(day=1), c["label"], c["amount"])
                await self.n.send_document([pid], png, f"parking-placeholder-{today:%Y-%m}.png",
                                           f"{c['label']} — {today:%B %Y} placeholder (not a receipt). "
                                           "Remember to download the official receipt.")
            except Exception as e:  # noqa: BLE001
                await self._report_error(f"placeholder for {pid}", e)

    # ---- Gmail: one IMAP login per check, shared by Redfin + card emails ----------------------
    def _read_mail(self, redfin: bool, card: bool) -> tuple[list[gmail.Mail], list[gmail.Mail]]:
        """Blocking. One Gmail session; runs only the searches requested. Returns (redfin, card) mails."""
        cfg = self.cfg
        if not (cfg.gmail_address and cfg.gmail_app_password):
            raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env")
        done = self.db.processed_email_ids()
        redfin_mails, card_mails = [], []
        with gmail.Session(cfg.gmail_address, cfg.gmail_app_password) as g:
            if redfin:
                em = cfg.settings["real_estate"]["email"]
                redfin_mails = g.search(["FROM", f'"{em["sender"]}"'], em["lookback_days"], done,
                                        (em.get("mailbox", "INBOX"),))
            if card:
                em = cfg.settings["cards"]["email"]
                card_mails = g.search(["SUBJECT", f'"{em["subject"]}"'], em["lookback_days"], done,
                                      tuple(em["mailboxes"]))
        return redfin_mails, card_mails

    async def mail_check(self, force: bool = False) -> None:
        """Scheduled Gmail check (every 2 hours): Redfin alerts and issuers' 5% emails, one login."""
        redfin = bool(self.access.recipients("realestate")) and self.cfg.settings["real_estate"].get("enabled")
        card = bool(self.access.recipients("cards")) and self.cfg.settings["cards"].get("enabled")
        if not (redfin or card):
            return
        try:
            redfin_mails, card_mails = await asyncio.to_thread(self._read_mail, redfin, card)
        except Exception as e:  # noqa: BLE001
            await self._report_error("gmail", e)
            return
        if redfin:
            await self.real_estate(force, mails=redfin_mails)
        if card:
            await self.card_emails(force, mails=card_mails)

    # Module B
    def _collect_listings(self, mails: list[gmail.Mail]) -> tuple[list[realestate.Listing], list[str]]:
        """Blocking: parse Redfin emails. Returns (listings, saved-search labels)."""
        cfg, em = self.cfg, self.cfg.settings["real_estate"]["email"]
        use_llm = em.get("llm_fallback") and cfg.gemini_api_key
        llm_fn = (lambda p: self._llm("extraction_models", p)) if use_llm else None
        listings, searches = [], []
        for m in mails:
            found = redfin_email.parse_email(m.subject, m.html, llm_fn)
            if not found:
                log.info("No listings in Redfin email %r", m.subject)
            listings += found
            if (meta := redfin_email.parse_search_meta(m.html)) and meta not in searches:
                searches.append(meta)
        return listings, searches

    MAX_PHOTO_ALERTS = 8  # per person per check; beyond that, captions are sent as plain text

    async def real_estate(self, force: bool = False, mails: list[gmail.Mail] | None = None) -> None:
        """New Redfin emails -> stored events -> delivered per person. Silent when nothing is new.
        `mails` comes from mail_check(); /run realestate reads Gmail itself."""
        re_cfg = self.cfg.settings["real_estate"]
        try:
            if mails is None:
                mails, _ = await asyncio.to_thread(self._read_mail, True, False)
            new_events = 0
            if mails:
                raw, searches = await asyncio.to_thread(self._collect_listings, mails)
                listings = realestate.filter_listings(raw, re_cfg["zips"], re_cfg.get("max_price"))
                diff = realestate.diff_and_store(self.db, listings)
                resolved = {l.id: l for l in await asyncio.to_thread(redfin_email.with_resolved_links, diff.reported())}
                for l in resolved.values():
                    self.db.update_listing_url(l.id, l.url)
                diff.new = [resolved[l.id] for l in diff.new]
                diff.price_changes = [(resolved[l.id], old) for l, old in diff.price_changes]
                diff.back_on_market = [resolved[l.id] for l in diff.back_on_market]
                diff.sold = [resolved[l.id] for l in diff.sold]
                diff.similar = [resolved[l.id] for l in diff.similar]
                search = searches[0] if len(searches) == 1 else ""
                meta = lambda l: {"zip": l.zip, "city": l.city, "price": l.price, "search": search}  # noqa: E731
                for l, cap in realestate.alert_items(diff, search):
                    new_events += self.db.add_event(f"{l.id}:{l.kind}:{l.price}",
                                                    {"type": "photo", "caption": cap, "photo": l.photo, **meta(l)})
                for l, group, line in realestate.summary_items(diff):
                    new_events += self.db.add_event(f"{l.id}:{group}:{l.price}",
                                                    {"type": "line", "group": group, "line": line, **meta(l)})
                # Events are stored, so the emails are done even if a delivery below fails.
                self.db.mark_emails_processed([m.message_id for m in mails])
            delivered = await self._deliver_realestate()
            if force and not delivered:
                note = f"{len(mails)} Redfin email(s), nothing new" if mails else "No new Redfin emails"
                await self.n.send_text(self.access.recipients("realestate"), f"🏡 {note} since the last check.")
            log.info("Real estate: %d emails, %d new events, %d deliveries", len(mails), new_events, delivered)
        except Exception as e:  # noqa: BLE001
            await self._report_error("realestate", e)

    async def _deliver_realestate(self) -> int:
        """Send each person the events they haven't received: new ones, plus a catch-up window for people who
        registered or resumed recently. A failure stops only that person's queue; it's retried next check."""
        days = self.cfg.settings["real_estate"].get("catch_up_days", 3)
        window_start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        photos: dict[str, bytes | None] = {}
        delivered, failed = 0, []
        for pid in self.access.recipients("realestate"):
            registered = self.db.registered_at(pid)
            if not registered or not self.db.chat_ids_for([pid]):
                continue                                   # not registered yet: their queue waits
            prefs = self.cfg.profiles.get(pid, {}).get("real_estate")
            sent_photos, lines, line_ids, searches = 0, {}, [], []
            for event_id, ev in self.db.undelivered_events(pid, max(window_start, registered)):
                if not realestate.matches_person(prefs, str(ev.get("zip", "")), ev.get("city", ""),
                                                 int(ev.get("price") or 0), ev.get("search", "")):
                    self.db.mark_delivered(pid, event_id)  # outside this person's own criteria: skip for good
                    continue
                if ev["type"] == "line":                   # grouped into one summary message below
                    lines.setdefault(ev["group"], []).append(ev["line"])
                    line_ids.append(event_id)
                    if ev.get("search") and ev["search"] not in searches:
                        searches.append(ev["search"])
                    continue
                try:
                    if ev["type"] == "photo":
                        url = ev.get("photo", "")
                        if url not in photos and self.cfg.settings["real_estate"].get("photos", True):
                            photos[url] = await asyncio.to_thread(redfin_email.fetch_photo, url)
                        if photos.get(url) and sent_photos < self.MAX_PHOTO_ALERTS:
                            await self.n.send_photo([pid], photos[url], ev["caption"])
                            sent_photos += 1
                        else:
                            await self.n.send_text([pid], ev["caption"])
                    else:
                        await self.n.send_text([pid], ev["text"])
                    self.db.mark_delivered(pid, event_id)
                    delivered += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("Delivery to %s failed at %s: %s", pid, event_id, e)
                    failed.append(f"{pid}: {e}")
                    lines, line_ids = {}, []               # retry everything for this person next check
                    break                                  # keep order; retry this person next check
            if line_ids:
                try:
                    await self.n.send_text([pid], realestate.format_summary(lines, searches))
                    for event_id in line_ids:
                        self.db.mark_delivered(pid, event_id)
                    delivered += len(line_ids)
                except Exception as e:  # noqa: BLE001
                    log.warning("Summary to %s failed: %s", pid, e)
                    failed.append(f"{pid}: {e}")
        if failed:
            await self._report_error("realestate delivery", RuntimeError("; ".join(failed)))
        return delivered

    async def mortgage(self, force: bool = False) -> None:
        try:
            rates = await asyncio.to_thread(mortgage.fetch_rates, self.cfg.settings["mortgage"]["series"])
            await self.n.send_text(self.access.recipients("realestate"), mortgage.format_rates(rates))
        except Exception as e:  # noqa: BLE001
            await self._report_error("mortgage", e)

    # Module C
    def seed_ticker_lists(self) -> None:
        for pid, lists in (self.cfg.settings["market"].get("seed_lists") or {}).items():
            if pid in self.cfg.profiles and not self.db.has_any_tickers(pid):
                for list_name in ("portfolio", "watch"):
                    self.db.add_tickers(pid, list_name, [s.upper() for s in lists.get(list_name) or []])
                log.info("Seeded ticker lists for %s", pid)

    def _brief_builder(self) -> brief.BriefBuilder:
        s = self.cfg.settings
        summarize = None
        if s["market"].get("llm", {}).get("enabled") and self.cfg.gemini_api_key:
            summarize = lambda p: self._llm("brief_models", p, json_output=True, retries=True)  # noqa: E731
        return brief.BriefBuilder(s, summarize, self.cfg.tz, self._name_cache)

    BRIEF_CACHE_SECONDS = 30 * 60

    def brief_for(self, profile: str, builder: brief.BriefBuilder | None = None, use_cache: bool = False,
                  mode: str = "morning") -> str:
        """Blocking: build one person's brief (morning | evening). use_cache: reuse one built <30 min ago."""
        lists = (tuple(self.db.tickers(profile, "portfolio")), tuple(self.db.tickers(profile, "watch")))
        hit = self._brief_cache.get(f"{profile}:{mode}")
        if use_cache and hit and hit[1] == lists and time.time() - hit[0] < self.BRIEF_CACHE_SECONDS:
            log.info("Serving cached brief for %s", profile)
            return hit[2]
        builder = builder or self._brief_builder()
        text = builder.build(list(lists[0]), list(lists[1]), mode)
        self._brief_cache[f"{profile}:{mode}"] = (time.time(), lists, text)
        return text

    async def evening(self, force: bool = False) -> None:
        await self.market(force, mode="evening")

    async def market(self, force: bool = False, mode: str = "morning") -> None:
        s = self.cfg.settings
        try:
            if not force and await asyncio.to_thread(brief.last_trading_day) != datetime.now(brief.NY).date():
                log.info("No US trading session today; skipping brief")
                return
            builder = self._brief_builder()  # shared: each ticker/feed fetched once per run
            failed = []
            for pid in self.access.recipients("market"):
                if not self.db.chat_ids_for([pid]):
                    continue
                try:                         # one person's failure must not cost the others their brief
                    text = await asyncio.to_thread(self.brief_for, pid, builder, False, mode)
                    await self.n.send_text([pid], text)
                except Exception as e:  # noqa: BLE001
                    log.warning("Brief for %s failed: %s", pid, e)
                    failed.append(f"{pid}: {e}")
            if failed:
                raise RuntimeError("; ".join(failed))
        except Exception as e:  # noqa: BLE001
            await self._report_error(f"market {mode}", e)

    # Module D
    async def _send_isolated(self, pid: str, text: str, markup=None) -> None:
        """Send to one person; a failure is logged + alerted but doesn't stop the loop over people."""
        try:
            await self.n.send_text([pid], text, markup)
        except Exception as e:  # noqa: BLE001
            await self._report_error(f"send to {pid}", e)

    def _activation_markup(self, pid: str, today: date):
        return buttons(cards.activation_buttons(self.cfg.cards, pid, today, self.db))

    def _card_owners(self, issuer: str) -> list[str]:
        return sorted({c["owner"] for c in self.cfg.cards.values()
                       if (c.get("rotating") or {}).get("issuer") == issuer and self.access.receives(c["owner"], "cards")})

    def _collect_card_emails(self, mails: list[gmail.Mail]) -> list[rotating.QuarterInfo]:
        """Blocking: parse 5%-category emails, merge into the DB. Returns quarters that are new or changed."""
        cfg, em = self.cfg, self.cfg.settings["cards"]["email"]
        # Forwarding address -> whose mail it is (profiles.<id>.email_from in settings.local.yaml).
        owners_by_addr = {a.lower(): pid for pid, p in cfg.profiles.items() for a in p.get("email_from", [])}
        allowed = [s.lower() for s in em.get("senders", []) + em.get("forward_from", []) + list(owners_by_addr)]
        llm_fn = (lambda p: self._llm("extraction_models", p, json_output=True)) if cfg.gemini_api_key else None
        changed: list[rotating.QuarterInfo] = []
        for m in mails:
            sender = m.sender.lower()
            if not any(a in sender for a in allowed):
                log.info("Ignoring 5%% email from unlisted sender %r", m.sender)
                continue
            owner = self._email_owner(sender, owners_by_addr)
            for q in rotating.parse_email(m.sender, m.subject, m.html, llm_fn).quarters:
                old = cards.load_email_quarter(self.db, q.issuer, q.quarter)
                merged = rotating.merge(old, q)
                cards.save_email_quarter(self.db, merged)                  # shared: public categories
                status_changed = False
                if owner:                                                   # personal: only the owner's status
                    status_changed = cards.save_owner_status(self.db, owner, q)
                elif q.activated is not None or q.activate_url:
                    log.info("5%% email from %r has no known owner; activation status not recorded", m.sender)
                if old is None or old.categories != merged.categories or status_changed:
                    changed.append(merged)
                log.info("%s %s: %s (owner=%s, activated=%s, preview=%s)", q.issuer, q.quarter, merged.categories,
                         owner, q.activated, merged.preview)
        return changed

    def migrate_card_status(self) -> int:
        """One-time: older versions stored activation status + link inside the shared per-issuer record.
        Move them to the card owner (only when exactly one person holds that issuer's card)."""
        moved = 0
        for k, v in self.db.kv_like("rotq:%"):
            data = json.loads(v)
            if data.get("activated") is None and not data.get("activate_url"):
                continue
            info = rotating.QuarterInfo(**data)
            owners = {c["owner"] for c in self.cfg.cards.values() if (c.get("rotating") or {}).get("issuer") == info.issuer}
            if len(owners) == 1:
                cards.save_owner_status(self.db, owners.pop(), info)
            cards.save_email_quarter(self.db, info)          # rewrite shared record without personal fields
            moved += 1
        return moved

    def _email_owner(self, sender: str, owners_by_addr: dict[str, str]) -> str | None:
        """Whose 5% email is this? The forwarding address decides; mail straight from a bank is attributed
        only if exactly one person has the bot's card feature (otherwise it's ambiguous)."""
        for addr, pid in owners_by_addr.items():
            if addr in sender:
                return pid
        holders = [pid for pid in self.cfg.profiles if self.access.allowed(pid) and "cards" in self.access.allowed(pid)
                   and any(c.get("owner") == pid for c in self.cfg.cards.values())]
        return holders[0] if len(holders) == 1 else None

    async def card_emails(self, force: bool = False, mails: list[gmail.Mail] | None = None) -> None:
        """Issuers' 5% emails -> tell card owners when a quarter's categories appear/change.
        `mails` comes from mail_check(); /run cardmail reads Gmail itself."""
        try:
            if mails is None:
                _, mails = await asyncio.to_thread(self._read_mail, False, True)
            ids = [m.message_id for m in mails]
            changed = await asyncio.to_thread(self._collect_card_emails, mails)
            if changed or force:
                today = self._today()
                owners = {o for q in changed for o in self._card_owners(q.issuer)} or (
                    set(self.access.recipients("cards")) if force else set())
                for pid in sorted(owners):
                    msg = cards.quarterly_message(self.cfg.cards, pid, self.cfg.profiles[pid]["name"], today, self.db)
                    if msg:
                        head = "📬 <b>New 5% info from your card emails</b>\n\n" if changed else ""
                        await self._send_isolated(pid, head + msg, self._activation_markup(pid, today))
            self.db.mark_emails_processed(ids)
        except Exception as e:  # noqa: BLE001
            await self._report_error("card_emails", e)

    async def quarterly_cards(self, force: bool = False) -> None:
        """1st of each quarter: this quarter's categories + activation status, per person."""
        try:
            today = self._today()
            for pid in self.access.recipients("cards"):
                if msg := cards.quarterly_message(self.cfg.cards, pid, self.cfg.profiles[pid]["name"], today, self.db):
                    await self._send_isolated(pid, msg, self._activation_markup(pid, today))
        except Exception as e:  # noqa: BLE001
            await self._report_error("quarterly_cards", e)

    async def activation_reminder(self, force: bool = False) -> None:
        """Mid-month nudge, only for cards whose quarter isn't confirmed activated."""
        try:
            today = self._today()
            for pid in self.access.recipients("cards"):
                if pending := cards.needs_activation(self.cfg.cards, pid, today, self.db):
                    msg = cards.quarterly_message(self.cfg.cards, pid, self.cfg.profiles[pid]["name"], today, self.db)
                    await self._send_isolated(pid, "⏰ <b>Reminder:</b> activate 5% on " + html.escape(", ".join(pending))
                                              + "\n\n" + (msg or ""), self._activation_markup(pid, today))
        except Exception as e:  # noqa: BLE001
            await self._report_error("activation_reminder", e)

    # VM health (admin only)
    PROCESS_START = time.time()

    def health_snapshot(self, with_oci: bool = True) -> vmhealth.Metrics:
        """Blocking."""
        m = vmhealth.collect(self.db, Path(os.environ.get("NARI_DATA_DIR", "/app/data")), BACKUP_DIR, self.PROCESS_START)
        if with_oci and self.cfg.settings.get("vm_health", {}).get("oci", True):
            m.oci = vmhealth.oci_metrics()
        return m

    async def health_check(self, force: bool = False) -> None:
        """Every 10 min: alarm once when a threshold is crossed, once when it recovers. Silent otherwise."""
        admins = self.access.recipients("vmhealth")
        if not admins:
            return
        try:
            t = self.cfg.settings["vm_health"]["thresholds"]
            m = await asyncio.to_thread(self.health_snapshot, True)
            changes = vmhealth.transitions(self.db, vmhealth.conditions(m, t))
            for key, firing, desc in changes:
                text = (f"🚨 <b>VM alarm</b>: {html.escape(desc)}" if firing else
                        f"✅ <b>Recovered</b>: {html.escape(key)} back to normal")
                for pid in admins:
                    await self._send_isolated(pid, text)
            if force and not changes:
                for pid in admins:
                    await self._send_isolated(pid, "🩺 Health check: no alarm changes.")
        except Exception as e:  # noqa: BLE001
            log.exception("health check failed")
            vmhealth.record_failure(self.db, "health_check", str(e))

    # Maintenance
    async def cleanup(self, force: bool = False) -> None:
        """Nightly: retention + leftovers; VACUUM on Sundays."""
        try:
            pruned = await asyncio.to_thread(maintenance.prune, self.db)
            fixed = await asyncio.to_thread(maintenance.one_time_cleanup, self.db)
            if self._today().weekday() == 6 or force:
                await asyncio.to_thread(maintenance.vacuum, self.db)
            log.info("Cleanup: pruned %s, fixed %s", pruned, fixed)
            if force:
                await self.n.send_text([self.cfg.primary_profile], f"🧹 Cleanup: pruned {pruned}; leftovers {fixed}")
        except Exception as e:  # noqa: BLE001
            await self._report_error("cleanup", e)

    async def backup(self, force: bool = False) -> None:
        try:
            keep = self.cfg.settings.get("maintenance", {}).get("backup_keep", 8)
            path = await asyncio.to_thread(maintenance.backup, self.db, BACKUP_DIR, keep)
            log.info("Backup written: %s", path)
            if force:
                await self.n.send_text([self.cfg.primary_profile], f"💾 Backup written: {path.name}")
        except Exception as e:  # noqa: BLE001
            await self._report_error("backup", e)

    def registry(self) -> dict:
        return {
            "placeholder": self.placeholder,
            "mail": self.mail_check,
            "realestate": self.real_estate,
            "mortgage": self.mortgage,
            "market": self.market,
            "evening": self.evening,
            "cards": self.quarterly_cards,
            "cardmail": self.card_emails,
            "activation": self.activation_reminder,
            "cleanup": self.cleanup,
            "health": self.health_check,
            "backup": self.backup,
        }
