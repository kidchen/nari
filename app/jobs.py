"""Scheduled jobs. Blocking network work runs in threads so the bot stays responsive."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime

from app.config import Config
from app.db import DB
from app.modules import cards, gmail, llm, market, mortgage, placeholder, realestate, redfin_email
from app.notify import Notifier

log = logging.getLogger(__name__)


class Jobs:
    def __init__(self, cfg: Config, db: DB, notifier: Notifier):
        self.cfg, self.db, self.n = cfg, db, notifier

    def _today(self) -> date:
        return datetime.now(self.cfg.tz).date()

    async def _report_error(self, job: str, e: Exception) -> None:
        log.exception("Job %s failed", job)
        await self.n.send_text([self.cfg.primary_profile],
                               f"⚠️ Job <b>{job}</b> failed: {html.escape(str(e))[:500]}")

    # Module A
    async def placeholder(self, force: bool = False) -> None:
        c = self.cfg.settings["placeholder"]
        try:
            today = self._today()
            png = await asyncio.to_thread(placeholder.render, today.replace(day=1), c["label"], c["amount"])
            await self.n.send_document(c["recipients"], png, f"parking-placeholder-{today:%Y-%m}.png",
                                       f"{c['label']} — {today:%B %Y} placeholder (not a receipt). "
                                       "Remember to download the official receipt.")
        except Exception as e:  # noqa: BLE001
            await self._report_error("placeholder", e)

    # Module B
    def _collect_listings(self) -> tuple[list[realestate.Listing], list[str], list[str]]:
        """Blocking: read new Redfin emails, parse them. Returns (listings, message_ids, errors)."""
        re_cfg, cfg = self.cfg.settings["real_estate"], self.cfg
        if not (cfg.gmail_address and cfg.gmail_app_password):
            return [], [], ["GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env"]
        em = re_cfg["email"]
        mails = gmail.fetch_mail(cfg.gmail_address, cfg.gmail_app_password, em["sender"],
                                 em["lookback_days"], self.db.processed_email_ids(), em.get("mailbox", "INBOX"))
        use_llm = em.get("llm_fallback") and cfg.gemini_api_key
        llm_fn = (lambda p: llm.summarize(cfg.gemini_api_key, self.cfg.settings["llm"].get("extraction_models") or self.cfg.settings["llm"]["models"], p)) \
            if use_llm else None
        listings, errors = [], []
        for m in mails:
            found = redfin_email.parse_email(m.subject, m.html, llm_fn)
            if not found:
                log.info("No listings in Redfin email %r", m.subject)
            listings += found
        return listings, [m.message_id for m in mails], errors

    async def real_estate(self, force: bool = False) -> None:
        s = self.cfg.settings
        re_cfg = s["real_estate"]
        try:
            raw, mail_ids, errors = await asyncio.to_thread(self._collect_listings)
            listings = realestate.filter_listings(raw, re_cfg["zips"], re_cfg["max_price"])
            diff = realestate.diff_and_store(self.db, listings)
            text = realestate.format_report(diff, re_cfg["max_price"], len(mail_ids), errors)
            try:
                rates = await asyncio.to_thread(mortgage.fetch_rates, s["mortgage"]["series"])
                text = mortgage.format_rates(rates) + "\n\n" + text
            except Exception as e:  # noqa: BLE001
                text = f"🏦 Mortgage rates unavailable: {html.escape(str(e))}\n\n" + text
            await self.n.send_text(re_cfg["recipients"], text)
            self.db.mark_emails_processed(mail_ids)  # only after the report went out
        except Exception as e:  # noqa: BLE001
            await self._report_error("real_estate", e)

    # Module C
    async def market(self, force: bool = False) -> None:
        s = self.cfg.settings
        try:
            text = await asyncio.to_thread(market.build_digest, s, self.cfg.gemini_api_key, force)
            if text:
                await self.n.send_text(s["market"]["recipients"], text)
        except Exception as e:  # noqa: BLE001
            await self._report_error("market", e)

    # Module D
    async def quarterly_cards(self, force: bool = False) -> None:
        try:
            today = self._today()
            for pid, p in self.cfg.profiles.items():
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
            "market": self.market,
            "cards": self.quarterly_cards,
            "offers": self.offer_reminders,
        }
