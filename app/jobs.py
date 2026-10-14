"""Scheduled jobs. Blocking network work runs in threads so the bot stays responsive."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import date, datetime

from app.access import Access
from app.config import Config
from app.db import DB
from app.modules import brief, cards, gmail, llm, mortgage, placeholder, realestate, redfin_email
from app.notify import Notifier

log = logging.getLogger(__name__)


class Jobs:
    def __init__(self, cfg: Config, db: DB, notifier: Notifier):
        self.cfg, self.db, self.n = cfg, db, notifier
        self._name_cache: dict[str, tuple[str, str]] = {}  # symbol -> (name, quote type), process lifetime
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
        c = self.cfg.settings["placeholder"]
        try:
            today = self._today()
            png = await asyncio.to_thread(placeholder.render, today.replace(day=1), c["label"], c["amount"])
            await self.n.send_document(self.access.recipients("placeholder"), png, f"parking-placeholder-{today:%Y-%m}.png",
                                       f"{c['label']} — {today:%B %Y} placeholder (not a receipt). "
                                       "Remember to download the official receipt.")
        except Exception as e:  # noqa: BLE001
            await self._report_error("placeholder", e)

    # Module B
    def _collect_listings(self) -> tuple[list[realestate.Listing], list[str], list[str], list[str]]:
        """Blocking: read new Redfin emails, parse them. Returns (listings, message_ids, searches, errors)."""
        re_cfg, cfg = self.cfg.settings["real_estate"], self.cfg
        if not (cfg.gmail_address and cfg.gmail_app_password):
            return [], [], [], ["GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env"]
        em = re_cfg["email"]
        mails = gmail.fetch_mail(cfg.gmail_address, cfg.gmail_app_password, em["sender"],
                                 em["lookback_days"], self.db.processed_email_ids(), em.get("mailbox", "INBOX"))
        use_llm = em.get("llm_fallback") and cfg.gemini_api_key
        llm_fn = (lambda p: self._llm("extraction_models", p)) if use_llm else None
        listings, searches, errors = [], [], []
        for m in mails:
            found = redfin_email.parse_email(m.subject, m.html, llm_fn)
            if not found:
                log.info("No listings in Redfin email %r", m.subject)
            listings += found
            if (meta := redfin_email.parse_search_meta(m.html)) and meta not in searches:
                searches.append(meta)
        return listings, [m.message_id for m in mails], searches, errors

    MAX_PHOTO_ALERTS = 8  # per poll; beyond that, listings go into the text summary

    async def real_estate(self, force: bool = False) -> None:
        """Poll Gmail for new Redfin emails (every 2 hours by default). Silent when nothing is new."""
        re_cfg = self.cfg.settings["real_estate"]
        recipients = self.access.recipients("realestate")
        try:
            raw, mail_ids, searches, errors = await asyncio.to_thread(self._collect_listings)
            if errors:
                raise RuntimeError("; ".join(errors))
            if not mail_ids:
                if force:
                    await self.n.send_text(recipients, "🏡 No new Redfin emails since the last check.")
                return
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
            items = realestate.alert_items(diff, search)
            overflow = []
            for i, (l, cap) in enumerate(items):
                photo = await asyncio.to_thread(redfin_email.fetch_photo, l.photo) \
                    if re_cfg.get("photos", True) and i < self.MAX_PHOTO_ALERTS else None
                if photo:
                    await self.n.send_photo(recipients, photo, cap)
                elif i < self.MAX_PHOTO_ALERTS:
                    await self.n.send_text(recipients, cap)
                else:
                    overflow.append(l)
            diff.similar = overflow + diff.similar if overflow else diff.similar
            if summary := realestate.summary_text(diff, searches):
                await self.n.send_text(recipients, summary)
            if force and not items and not summary:
                await self.n.send_text(recipients, f"🏡 {len(mail_ids)} Redfin email(s), nothing new to report.")
            self.db.mark_emails_processed(mail_ids)  # only after everything went out
        except Exception as e:  # noqa: BLE001
            await self._report_error("realestate", e)

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

    def brief_for(self, profile: str, builder: brief.BriefBuilder | None = None, use_cache: bool = False) -> str:
        """Blocking: build one person's brief. use_cache: reuse one built <30 min ago for the same lists."""
        lists = (tuple(self.db.tickers(profile, "portfolio")), tuple(self.db.tickers(profile, "watch")))
        hit = self._brief_cache.get(profile)
        if use_cache and hit and hit[1] == lists and time.time() - hit[0] < self.BRIEF_CACHE_SECONDS:
            log.info("Serving cached brief for %s", profile)
            return hit[2]
        builder = builder or self._brief_builder()
        text = builder.build(list(lists[0]), list(lists[1]))
        self._brief_cache[profile] = (time.time(), lists, text)
        return text

    async def market(self, force: bool = False) -> None:
        s = self.cfg.settings
        try:
            if not force and await asyncio.to_thread(brief.last_trading_day) != datetime.now(brief.NY).date():
                log.info("No US trading session today; skipping brief")
                return
            builder = self._brief_builder()  # shared: each ticker/feed fetched once per run
            for pid in self.access.recipients("market"):
                if not self.db.chat_ids_for([pid]):
                    continue
                text = await asyncio.to_thread(self.brief_for, pid, builder)
                await self.n.send_text([pid], text)
        except Exception as e:  # noqa: BLE001
            await self._report_error("market", e)

    # Module D
    async def quarterly_cards(self, force: bool = False) -> None:
        try:
            today = self._today()
            for pid, p in self.cfg.profiles.items():
                if not self.access.receives(pid, "cards"):
                    continue
                msg = cards.quarterly_message(self.cfg.cards, pid, p["name"], today,
                                              cards.load_overrides(self.db, today))
                if msg:
                    await self.n.send_text([pid], msg)
        except Exception as e:  # noqa: BLE001
            await self._report_error("quarterly_cards", e)

    async def offer_reminders(self, force: bool = False) -> None:
        try:
            today = self._today()
            days = self.cfg.settings["cards"]["offer_reminder_days"]
            for r, left in cards.expiring_offers(self.db.active_offers(today=today), today, days):
                if not self.access.receives(r["profile"], "cards"):
                    continue
                card = self.cfg.cards.get(r["card_id"], {}).get("name", r["card_id"])
                when = "tomorrow" if left == 1 else f"in {left} days"
                await self.n.send_text([r["profile"]],
                                       f"⏰ Offer #{r['id']} <b>{html.escape(r['merchant'])}</b> "
                                       f"({html.escape(card)}) expires {when}: {html.escape(r['description'])}")
        except Exception as e:  # noqa: BLE001
            await self._report_error("offer_reminders", e)

    def registry(self) -> dict:
        return {
            "placeholder": self.placeholder,
            "realestate": self.real_estate,
            "mortgage": self.mortgage,
            "market": self.market,
            "cards": self.quarterly_cards,
            "offers": self.offer_reminders,
        }
