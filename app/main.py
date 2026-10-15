"""Entry point: Telegram bot (long polling) + APScheduler cron jobs on the same event loop."""
from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
import re
import os
import time
from pathlib import Path
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app import maintenance
from app.config import Config, load_config
from app.db import DB
from app.jobs import Jobs
from app.modules import brief, cards, vmhealth
from app.notify import Notifier, split_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log bot token in request URLs
log = logging.getLogger("nari")
HEARTBEAT_FILE = Path(os.environ.get("NARI_HEARTBEAT_FILE", "/tmp/nari-heartbeat"))

# (command, menu description, extra usage lines) — /help shows one command per line; the extra lines
# document variants. Telegram's "/" menu shows only the base commands.
COMMANDS: dict[str | None, list[tuple[str, str, list[str]]]] = {
    None: [("features", "What you get, and what's paused", []),
           ("pause", "Pause a feature's scheduled messages", ["/pause market"]),
           ("resume", "Resume a paused feature", ["/resume market"])],
    "market": [("brief", "Market brief now: today before the close, close + look-ahead after",
                ["/brief morning · /brief evening — pick one", "/brief fresh — skip the 30-min cache"]),
               ("portfolio", "Show your portfolio", ["/portfolio add IBM KO — add stocks", "/portfolio del KO — remove"]),
               ("watch", "Show your watchlist", ["/watch add XOM — add stocks", "/watch del XOM — remove"])],
    "vmhealth": [("health", "VM + bot health: disk, memory, load, backups, job failures, OCI metrics", [])],
    "cards": [("cards", "This quarter's 5% categories and activation status", []),
              ("best", "Which of your cards to use for a category", ["/best dining"]),
              ("setcats", "Correct a quarter's 5% categories by hand", ["/setcats discover_it Gas, Transit"])],
}
ADMIN_COMMANDS = [("status", "People, features, AI model health, next runs", []),
                  ("run", "Run a scheduled job now",
                   ["/run mail | realestate | cardmail | mortgage | market | evening | cards | activation | placeholder | cleanup | backup"])]


class BotApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.app = Application.builder().token(cfg.telegram_token).post_init(self._post_init).build()
        self.jobs: Jobs | None = None
        self.scheduler = AsyncIOScheduler(timezone=cfg.tz)
        for name, fn in [("start", self.start), ("help", self.help),
                         ("cards", self.cards), ("best", self.best), ("setcats", self.setcats), ("run", self.run),
                         ("brief", self.brief), ("portfolio", self.portfolio), ("watch", self.watch),
                         ("features", self.features), ("pause", self.pause), ("resume", self.resume),
                         ("status", self.status), ("health", self.health)]:
            self.app.add_handler(CommandHandler(name, fn))
        self.app.add_handler(CallbackQueryHandler(self.pick_profile, pattern=r"^reg:"))
        self.app.add_handler(CallbackQueryHandler(self.confirm_activation, pattern=r"^act:"))

    async def _post_init(self, app: Application) -> None:
        self.jobs = Jobs(self.cfg, self.db, Notifier(app.bot, self.db))
        self.jobs.seed_ticker_lists()
        vmhealth.record_start(self.db)
        self._write_admin_chat()
        maintenance.check_email_retention(self.cfg.settings)   # refuse to start with an unsafe retention
        log.info("One-time cleanup: %s; card status moved to owners: %d", maintenance.one_time_cleanup(self.db),
                 self.jobs.migrate_card_status())
        for pid in self.cfg.profiles:
            for chat_id in self.db.chat_ids_for([pid]):
                await self._sync_menu(chat_id, pid)
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
            add("mail", j.mail_check, s["real_estate"]["alerts_schedule"])  # Redfin + card emails, one login
            add("mortgage", j.mortgage, s["mortgage"]["schedule"])
        if s["market"].get("enabled"):
            add("market", j.market, s["market"]["schedule"])
            add("evening", j.evening, s["market"]["evening_schedule"])
        if s["cards"].get("enabled"):
            add("cards", j.quarterly_cards, s["cards"]["quarterly_schedule"])
            add("activation", j.activation_reminder, s["cards"]["activation_reminder_schedule"])
        mt = s.get("maintenance", {})
        add("cleanup", j.cleanup, mt.get("cleanup_schedule", {"hour": 3, "minute": 0}))
        vh = s.get("vm_health", {})
        if vh.get("enabled", True):
            add("health", j.health_check, vh.get("schedule", {"minute": "*/10"}))
        add("backup", j.backup, mt.get("backup_schedule", {"day_of_week": "sun", "hour": 3, "minute": 30}))

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

    def _write_admin_chat(self) -> None:
        """The host watchdog (deploy/watchdog.sh) alerts this chat when the bot itself is down."""
        ids = self.db.chat_ids_for([self.cfg.primary_profile])
        if ids:
            (self.cfg.db_path.parent / "admin_chat_id").write_text(str(ids[0]))

    def _commands_for(self, pid: str) -> list[tuple[str, str, list[str]]]:
        allowed = self.jobs.access.allowed(pid) if self.jobs else []
        cmds = [c for feature, group in COMMANDS.items() if feature is None or feature in allowed for c in group]
        return cmds + (ADMIN_COMMANDS if pid == self.cfg.primary_profile else [])

    def _help(self, pid: str) -> str:
        lines = ["<b>Nari commands</b>"]
        for cmd, desc, usage in self._commands_for(pid):
            lines.append(f"/{cmd} — {html.escape(desc, quote=False)}")
            lines += [f"    {html.escape(u, quote=False)}" for u in usage]
        return "\n".join(lines)

    async def _sync_menu(self, chat_id: int, pid: str) -> None:
        """Per-person '/' menu in Telegram, matching what they're allowed to use."""
        try:
            await self.app.bot.set_my_commands([BotCommand(c, d[:256]) for c, d, _ in self._commands_for(pid)],
                                               scope=BotCommandScopeChat(chat_id))
        except Exception as e:  # noqa: BLE001 — cosmetic; never block startup/registration
            log.warning("Could not set command menu for %s: %s", pid, e)

    async def _feature_profile(self, update: Update, feature: str) -> str | None:
        """Registered profile that is allowed to use `feature` (pausing only stops scheduled messages)."""
        pid = await self._profile(update)
        if pid and self.jobs and feature not in self.jobs.access.allowed(pid):
            await self._reply(update, "That feature isn't enabled for you.")
            return None
        return pid

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
        self._write_admin_chat()
        await self._sync_menu(update.effective_chat.id, pid)
        await update.effective_chat.send_message(
            f"✅ Registered as <b>{html.escape(self.cfg.profiles[pid]['name'])}</b>.\n\n{self._help(pid)}",
            parse_mode=ParseMode.HTML)

    async def help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        pid = self.db.profile_for(update.effective_chat.id)
        await self._reply(update, self._help(pid) if pid else "Send /start &lt;invite_code&gt; to register.")

    async def cards(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if pid := await self._feature_profile(update, "cards"):
            msg = cards.quarterly_message(self.cfg.cards, pid, self.cfg.profiles[pid]["name"], self._today(), self.db)
            await update.effective_message.reply_text(
                msg or "No rotating 5% cards configured for you (config/cards.local.yaml).", parse_mode=ParseMode.HTML,
                disable_web_page_preview=True, reply_markup=self.jobs._activation_markup(pid, self._today()) if msg else None)

    async def best(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not (pid := await self._feature_profile(update, "cards")):
            return
        if not ctx.args:
            await self._reply(update, "Usage: /best &lt;category&gt;, e.g. /best groceries")
            return
        cat = " ".join(ctx.args)
        ranked = cards.best_card(self.cfg.cards, pid, cat, self._today(), self.db)
        if not ranked:
            await self._reply(update, "No cards configured for you.")
            return
        lines = [f"Best for <b>{html.escape(cat)}</b>:"]
        lines += [f"{i}. {html.escape(name)} — {rate:g}% ({reason})" for i, (rate, name, reason) in
                  enumerate(ranked, 1)]
        await self._reply(update, "\n".join(lines))

    async def confirm_activation(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """'✅ I activated <card>' button, for issuers that never email a confirmation (e.g. Discover)."""
        q = update.callback_query
        pid = self.db.profile_for(update.effective_chat.id)
        _, card_id, qkey = q.data.split(":", 2)
        card = self.cfg.cards.get(card_id)
        if not pid or not card or card.get("owner") != pid:
            await q.answer("That isn't one of your cards.", show_alert=True)
            return
        cards.mark_activated(self.db, card_id, qkey)
        log.info("%s confirmed %s activated for %s", pid, card_id, qkey)
        await q.answer(f"Marked {card['name']} as activated for {qkey}")
        today = self._today()
        msg = cards.quarterly_message(self.cfg.cards, pid, self.cfg.profiles[pid]["name"], today, self.db)
        try:  # refresh the message in place so its status and buttons update
            await q.edit_message_text(msg or "", parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                      reply_markup=self.jobs._activation_markup(pid, today) if self.jobs else None)
        except Exception:  # noqa: BLE001 — e.g. the button was on a reminder with extra text
            await q.edit_message_reply_markup(self.jobs._activation_markup(pid, today) if self.jobs else None)

    async def setcats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not (pid := await self._feature_profile(update, "cards")):
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

    # ---- market lists & brief --------------------------------------------------
    LIST_LABELS = {"portfolio": "💼 Portfolio", "watch": "👀 Watchlist"}
    SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,9}$")

    async def portfolio(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._edit_list(update, ctx, "portfolio")

    async def watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._edit_list(update, ctx, "watch")

    async def _edit_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, list_name: str) -> None:
        if not (pid := await self._feature_profile(update, "market")):
            return
        cmd, label = f"/{list_name}", self.LIST_LABELS[list_name]
        args = [a.strip(",").upper() for a in (ctx.args or []) if a.strip(",")]
        action, symbols = (args[0].lower(), args[1:]) if args else ("show", [])
        if action in ("add", "del", "rm", "remove") and not symbols:
            await self._reply(update, f"Usage: {cmd} {action} TICKER [TICKER ...]")
            return
        if action == "add":
            bad = [s for s in symbols if not self.SYMBOL_RE.match(s)]
            valid, invalid = await asyncio.to_thread(brief.validate_symbols, [s for s in symbols if s not in bad])
            added = self.db.add_tickers(pid, list_name, valid)
            msg = f"Added to {label}: {', '.join(added) or 'nothing new'}"
            if bad + invalid:
                msg += f"\nNot found on Yahoo: {html.escape(', '.join(bad + invalid))}"
        elif action in ("del", "rm", "remove"):
            removed = self.db.remove_tickers(pid, list_name, symbols)
            msg = f"Removed from {label}: {', '.join(removed) or 'none of those were on it'}"
        elif action == "show":
            msg = ""
        else:
            await self._reply(update, f"Usage: {cmd} | {cmd} add TICKER ... | {cmd} del TICKER ...")
            return
        current = self.db.tickers(pid, list_name)
        msg = (msg + "\n\n" if msg else "") + f"<b>{label}</b> ({len(current)}): " + (", ".join(current) or "empty")
        await self._reply(update, msg)

    async def brief(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not (pid := await self._feature_profile(update, "market")) or not self.jobs:
            return
        args = [a.lower() for a in ctx.args or []]
        fresh = "fresh" in args
        # Default by time of day: before the 1pm PT close -> today's market; after -> close + look ahead.
        mode = "evening" if "evening" in args else "morning" if "morning" in args else (
            "evening" if datetime.now(brief.NY).hour >= 16 else "morning")
        await self._reply(update, f"Building your {mode} brief… (about a minute)")
        try:
            text = await asyncio.to_thread(self.jobs.brief_for, pid, None, not fresh, mode)
        except Exception as e:  # noqa: BLE001
            log.exception("On-demand brief failed")
            await self._reply(update, f"Brief failed: {html.escape(str(e))[:300]}")
            return
        for chunk in split_message(text):
            await update.effective_chat.send_message(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    # ---- features / status --------------------------------------------------
    async def features(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if (pid := await self._profile(update)) and self.jobs:
            await self._reply(update, f"<b>{html.escape(self.cfg.profiles[pid]['name'])}</b> ({pid})\n"
                              + self.jobs.access.describe(pid)
                              + "\n\n/pause &lt;feature&gt; stops scheduled messages; /resume &lt;feature&gt; restarts them.")

    async def pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._toggle(update, ctx, paused=True)

    async def resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._toggle(update, ctx, paused=False)

    async def _toggle(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, paused: bool) -> None:
        if not (pid := await self._profile(update)) or not self.jobs:
            return
        allowed = self.jobs.access.allowed(pid)
        feature = (ctx.args or [""])[0].lower()
        if feature not in allowed:
            await self._reply(update, f"Usage: /{'pause' if paused else 'resume'} &lt;feature&gt;\nYours: "
                              + ", ".join(allowed))
            return
        self.jobs.access.set_paused(pid, feature, paused)
        log.info("%s %s %s", pid, "paused" if paused else "resumed", feature)
        await self._reply(update, self.jobs.access.describe(pid))

    async def health(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not (await self._feature_profile(update, "vmhealth")) or not self.jobs:
            return
        await self._reply(update, "Checking… (OCI metrics take a few seconds)")
        m = await asyncio.to_thread(self.jobs.health_snapshot, True)
        await self._reply(update, vmhealth.format_health(m, self.cfg.settings["vm_health"]["thresholds"]))

    async def status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not (pid := await self._profile(update)) or not self.jobs:
            return
        if pid != self.cfg.primary_profile:
            await self._reply(update, "Only the primary user can see status.")
            return
        lines = ["<b>People</b>"]
        for p, prof in self.cfg.profiles.items():
            reg = "registered" if self.db.chat_ids_for([p]) else "not registered"
            on = [f for f in self.jobs.access.allowed(p) if f not in self.jobs.access.paused(p)]
            off = self.jobs.access.paused(p)
            lines.append(f"• {html.escape(prof['name'])} ({reg}): " + (", ".join(on) or "nothing")
                         + (f" · paused: {', '.join(off)}" if off else ""))
        lines += ["", "<b>AI models</b>"] + [f"• {html.escape(x)}" for x in self.jobs.llm_health.summary()]
        lines += ["", "<b>Next runs</b>"] + [f"• {j.id}: {j.next_run_time:%a %b %d %H:%M}"
                                             for j in self.scheduler.get_jobs() if j.id != "heartbeat"]
        await self._reply(update, "\n".join(lines))

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
