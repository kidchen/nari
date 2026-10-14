"""Entry point: Telegram bot (long polling) + APScheduler cron jobs on the same event loop."""
from __future__ import annotations

import hmac
import html
import json
import logging
import os
import time
from pathlib import Path
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.config import Config, load_config
from app.db import DB
from app.jobs import Jobs
from app.modules import cards
from app.notify import Notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log bot token in request URLs
log = logging.getLogger("nari")
HEARTBEAT_FILE = Path(os.environ.get("NARI_HEARTBEAT_FILE", "/tmp/nari-heartbeat"))

HELP = """<b>Nari commands</b>
/whoami — your profile
/cards — your cards and this quarter's categories
/best &lt;category&gt; — which of your cards to use (e.g. /best groceries)
/setcats &lt;card_id&gt; Cat A, Cat B — set this quarter's rotating categories
/offers — your active targeted offers
/offer add &lt;card_id&gt; | &lt;merchant&gt; | &lt;description&gt; [| YYYY-MM-DD]
/offer done &lt;id&gt; — remove an offer
/run &lt;placeholder|realestate|market|cards|offers&gt; — run a job now (primary user only)"""


class BotApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.app = Application.builder().token(cfg.telegram_token).post_init(self._post_init).build()
        self.jobs: Jobs | None = None
        self.scheduler = AsyncIOScheduler(timezone=cfg.tz)
        for name, fn in [("start", self.start), ("help", self.help), ("whoami", self.whoami),
                         ("cards", self.cards), ("best", self.best), ("offers", self.offers),
                         ("offer", self.offer), ("setcats", self.setcats), ("run", self.run)]:
            self.app.add_handler(CommandHandler(name, fn))
        self.app.add_handler(CallbackQueryHandler(self.pick_profile, pattern=r"^reg:"))

    async def _post_init(self, app: Application) -> None:
        self.jobs = Jobs(self.cfg, self.db, Notifier(app.bot, self.db))
        self._schedule()
        self.scheduler.start()
        for job in self.scheduler.get_jobs():
            log.info("Scheduled %s next=%s", job.id, job.next_run_time)
        # Liveness signal for the Docker HEALTHCHECK / deploy.sh: written only once Telegram is
        # connected and the scheduler is running, then refreshed while the event loop is alive.
        self._heartbeat()
        self.scheduler.add_job(self._heartbeat, IntervalTrigger(seconds=30), id="heartbeat")

    @staticmethod
    def _heartbeat() -> None:
        HEARTBEAT_FILE.write_text(str(int(time.time())))

    def _schedule(self) -> None:
        s, j = self.cfg.settings, self.jobs
        assert j is not None

        def add(job_id: str, fn, spec: dict) -> None:
            self.scheduler.add_job(fn, CronTrigger(timezone=self.cfg.tz, **spec), id=job_id,
                                   misfire_grace_time=3600, coalesce=True, max_instances=1)

        if s["placeholder"].get("enabled"):
            add("placeholder", j.placeholder, s["placeholder"]["schedule"])
        if s["real_estate"].get("enabled"):
            add("realestate", j.real_estate, s["real_estate"]["schedule"])
        if s["market"].get("enabled"):
            add("market", j.market, s["market"]["schedule"])
        if s["cards"].get("enabled"):
            add("cards", j.quarterly_cards, s["cards"]["quarterly_schedule"])
            add("offers", j.offer_reminders, s["cards"]["offer_reminder_schedule"])

    # ---- helpers -------------------------------------------------------------
    async def _reply(self, update: Update, text: str) -> None:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                                  disable_web_page_preview=True)

    async def _profile(self, update: Update) -> str | None:
        pid = self.db.profile_for(update.effective_chat.id)
        if not pid:
            await self._reply(update, "Not registered. Send /start &lt;invite_code&gt;.")
        return pid

    def _today(self):
        return datetime.now(self.cfg.tz).date()

    # ---- commands ------------------------------------------------------------
    INVITE_TTL = 600  # seconds a verified invite code stays valid for picking a profile

    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/start <code> shows profile buttons; /start <code> <profile> registers directly."""
        args = ctx.args or []
        if not args:
            await self._reply(update, "Send: /start &lt;invite_code&gt;")
            return
        if not hmac.compare_digest(args[0], self.cfg.invite_code):
            log.warning("Bad invite code from chat %s", update.effective_chat.id)
            await self._reply(update, "Invalid invite code.")
            return
        try:  # the message contains the invite code; remove it from chat history if possible
            await update.effective_message.delete()
        except Exception:  # noqa: BLE001 — bots can't always delete user messages
            pass
        if len(args) >= 2:
            pid = args[1].lower()
            if pid not in self.cfg.profiles:
                await update.effective_chat.send_message("Unknown profile. Options: " + ", ".join(self.cfg.profiles))
                return
            await self._register(update, pid)
            return
        ctx.user_data["invite_ok_until"] = time.time() + self.INVITE_TTL
        buttons = [[InlineKeyboardButton(p["name"], callback_data=f"reg:{pid}")]
                   for pid, p in self.cfg.profiles.items()]
        await update.effective_chat.send_message("Who are you?", reply_markup=InlineKeyboardMarkup(buttons))

    async def pick_profile(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        pid = q.data.split(":", 1)[1]
        if ctx.user_data.get("invite_ok_until", 0) < time.time() or pid not in self.cfg.profiles:
            await q.edit_message_text("That registration link expired. Send /start <invite_code> again.")
            return
        ctx.user_data.pop("invite_ok_until", None)
        await q.edit_message_reply_markup(None)
        await self._register(update, pid)

    async def _register(self, update: Update, pid: str) -> None:
        user = update.effective_user
        self.db.register_user(update.effective_chat.id, pid, user.full_name if user else "")
        log.info("Registered chat %s as %s", update.effective_chat.id, pid)
        await update.effective_chat.send_message(
            f"✅ Registered as <b>{html.escape(self.cfg.profiles[pid]['name'])}</b>.\n\n{HELP}",
            parse_mode=ParseMode.HTML)

    async def help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, HELP)

    async def whoami(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if pid := await self._profile(update):
            await self._reply(update, f"{html.escape(self.cfg.profiles[pid]['name'])} ({pid}), "
                                      f"chat_id {update.effective_chat.id}")

    async def cards(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if pid := await self._profile(update):
            msg = cards.quarterly_message(self.cfg.cards, pid, self.cfg.profiles[pid]["name"], self._today(),
                                          cards.load_overrides(self.db, self._today()))
            await self._reply(update, msg or "No cards configured for you in config/cards.yaml.")

    async def best(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not (pid := await self._profile(update)):
            return
        if not ctx.args:
            await self._reply(update, "Usage: /best &lt;category&gt;, e.g. /best groceries")
            return
        cat = " ".join(ctx.args)
        ranked = cards.best_card(self.cfg.cards, pid, cat, self._today(),
                                 cards.load_overrides(self.db, self._today()))
        if not ranked:
            await self._reply(update, "No cards configured for you.")
            return
        lines = [f"Best for <b>{html.escape(cat)}</b>:"]
        lines += [f"{i}. {html.escape(name)} — {rate:g}% ({reason})" for i, (rate, name, reason) in
                  enumerate(ranked, 1)]
        offers = [o for o in self.db.active_offers(pid, self._today()) if cat.lower() in o["merchant"].lower()]
        lines += [f"🎯 Offer #{o['id']}: {html.escape(o['merchant'])} — {html.escape(o['description'])}"
                  for o in offers]
        await self._reply(update, "\n".join(lines))

    async def offers(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if pid := await self._profile(update):
            await self._reply(update, cards.format_offers(self.db.active_offers(pid, self._today()),
                                                          self.cfg.cards))

    async def offer(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not (pid := await self._profile(update)):
            return
        args = ctx.args or []
        if args[:1] == ["add"]:
            text = update.effective_message.text.split(None, 2)[2] if len(args) > 1 else ""
            try:
                o = cards.parse_offer_args(text, cards.cards_for(self.cfg.cards, pid))
            except cards.OfferParseError as e:
                await self._reply(update, str(e))
                return
            oid = self.db.add_offer(pid, o.card_id, o.merchant, o.description, o.expires)
            await self._reply(update, f"Saved offer #{oid}: <b>{html.escape(o.merchant)}</b> — "
                                      f"{html.escape(o.description)}")
        elif args[:1] == ["done"] and len(args) == 2 and args[1].isdigit():
            ok = self.db.deactivate_offer(int(args[1]), pid)
            await self._reply(update, "Removed." if ok else "No such active offer of yours.")
        else:
            await self._reply(update, cards.OFFER_USAGE + "\n/offer done &lt;id&gt;")

    async def setcats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not (pid := await self._profile(update)):
            return
        args = ctx.args or []
        rotating = {cid: c for cid, c in self.cfg.cards.items() if c.get("rotating")}
        if len(args) < 2 or args[0] not in rotating:
            await self._reply(update, "Usage: /setcats &lt;card_id&gt; Category A, Category B\nRotating cards: "
                              + (", ".join(rotating) or "none"))
            return
        cats = [c.strip() for c in " ".join(args[1:]).split(",") if c.strip()]
        qkey = cards.quarter_key(self._today())
        self.db.set_kv(cards.override_key(args[0], qkey), json.dumps(cats))
        log.info("%s set %s %s categories: %s", pid, args[0], qkey, cats)
        await self._reply(update, f"Saved {qkey} categories for {html.escape(rotating[args[0]]['name'])}: "
                                  + html.escape(", ".join(cats)))

    async def run(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not (pid := await self._profile(update)):
            return
        if pid != self.cfg.primary_profile:
            await self._reply(update, "Only the primary user can run jobs.")
            return
        reg = self.jobs.registry() if self.jobs else {}
        name = (ctx.args or [""])[0]
        if name not in reg:
            await self._reply(update, "Jobs: " + ", ".join(reg))
            return
        await self._reply(update, f"Running {name}…")
        await reg[name](force=True)


def main() -> None:
    cfg = load_config()
    BotApp(cfg).app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
