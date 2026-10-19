"""Entry point: Telegram bot (long polling) + APScheduler cron jobs on the same event loop.

Feature-agnostic: commands, callbacks, aliases, schedules, /help, menus, alarms and /status sections all
come from the feature registry (app/features/__init__.py). Only registration, /features, /status and /run
live here; job runs and alarms are in app/monitoring.py.
"""
from __future__ import annotations

import hmac
import html
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app import maintenance, monitoring
from app.access import catalog
from app.config import Config, load_config
from app.db import DB
from app.features.vmhealth.vmhealth import record_start
from app.notify import Notifier, split_message
from app.registry import Feature
from app.services import Services

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log bot token in request URLs
log = logging.getLogger("nari")
HEARTBEAT_FILE = Path(os.environ.get("NARI_HEARTBEAT_FILE", "/tmp/nari-heartbeat"))

# Core commands: (name, description, usage). Feature commands come from the registry.
CORE_COMMANDS = [("help", "Your commands", ()), ("features", "What you get — tap to pause/resume", ())]
ADMIN_COMMANDS = [("status", "App status: alarms, jobs, people, AI models", ()),
                  ("run", "Run a scheduled job now", ("/run <job> — /run alone lists the jobs",))]
CORE_ALIASES = {"pause": "/features (tap a feature)", "resume": "/features (tap a feature)"}


class BotApp:
    def __init__(self, cfg: Config, features: list[Feature] | None = None):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.app = Application.builder().token(cfg.telegram_token).post_init(self._post_init).build()
        self.services = Services(cfg, self.db, None, features)
        self.scheduler = AsyncIOScheduler(timezone=cfg.tz)
        for name, fn in [("start", self.start), ("help", self.help), ("features", self.features),
                         ("status", self.status), ("run", self.run), ("pause", self.pause), ("resume", self.resume)]:
            self.app.add_handler(CommandHandler(name, fn))
        for f in self.services.features:
            for c in f.commands:
                self.app.add_handler(CommandHandler(c.name, self._bind(c.handler)))
            for a in f.aliases:
                self.app.add_handler(CommandHandler(a.name, self._bind_alias(a.handler, a.name, a.hint)))
            for pattern, handler in f.callbacks:
                self.app.add_handler(CallbackQueryHandler(self._bind(handler), pattern=pattern))
        self.app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        self.app.add_handler(CallbackQueryHandler(self.pick_profile, pattern=r"^reg:"))
        self.app.add_error_handler(self._on_error)
        self.app.add_handler(CallbackQueryHandler(self.toggle_feature, pattern=r"^feat:"))

    def _bind(self, handler):
        async def run(update, ctx):
            await handler(self, update, ctx)
        return run

    def _bind_alias(self, handler, old: str, new: str):
        async def run(update, ctx):
            await handler(self, update, ctx)
            await self._reply(update, f"<i>Tip: /{old} is now {html.escape(new)}.</i>")
        return run

    async def _post_init(self, app: Application) -> None:
        self.services.n = Notifier(app.bot, self.db)
        maintenance.check_email_retention(self.cfg.settings)   # refuse to start with an unsafe retention
        record_start(self.db)
        self._write_admin_chat()
        log.info("One-time cleanup: %s", maintenance.one_time_cleanup(self.db))
        from app.services import BACKUP_DIR
        recipient = self.cfg.settings.get("maintenance", {}).get("backup_age_recipient")
        if n := maintenance.encrypt_old_backups(BACKUP_DIR, recipient):
            log.info("Encrypted %d old plaintext backup(s)", n)
        for f in self.services.features:
            if f.on_startup:
                f.on_startup(self.services)
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
    async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Telegram's polling hits brief 'Bad Gateway' / timeouts that the library retries by itself:
        log one line for those; keep full tracebacks for real bugs."""
        from telegram.error import NetworkError, TimedOut
        if isinstance(ctx.error, NetworkError | TimedOut):
            log.warning("Telegram network blip (auto-retried): %s", ctx.error)
        else:
            log.error("Unhandled error in update %s", update, exc_info=ctx.error)

    @staticmethod
    def _heartbeat() -> None:
        HEARTBEAT_FILE.write_text(str(int(time.time())))

    def _schedule(self) -> None:
        s = self.services
        for job_id, fn, kwargs, spec in s.scheduled_jobs():      # each wrapped in the run ledger
            self.scheduler.add_job(fn, monitoring.make_trigger(spec, self.cfg.tz), args=[s], kwargs=kwargs, id=job_id,
                                   misfire_grace_time=3600, coalesce=True, max_instances=1)

    # ---- helpers (also used by feature handlers) --------------------------------------------
    async def _reply(self, update: Update, text: str) -> None:
        for chunk in split_message(text):
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    async def _profile(self, update: Update) -> str | None:
        pid = self.db.profile_for(update.effective_chat.id)
        if not pid:
            await self._reply(update, "Not registered. Send /start &lt;invite_code&gt;.")
        return pid

    def _today(self):
        return datetime.now(self.cfg.tz).date()

    async def _feature_profile(self, update: Update, feature: str) -> str | None:
        """Registered profile that is allowed to use `feature` (pausing only stops scheduled messages)."""
        pid = await self._profile(update)
        if pid and feature not in self.services.access.allowed(pid):
            await self._reply(update, "That feature isn't enabled for you.")
            return None
        return pid

    def _write_admin_chat(self) -> None:
        """The host watchdog (deploy/watchdog.sh) alerts this chat when the bot itself is down."""
        ids = self.db.chat_ids_for([self.cfg.primary_profile])
        if ids:
            (self.cfg.db_path.parent / "admin_chat_id").write_text(str(ids[0]))

    def _ready(self, pid: str, feature: Feature) -> bool:
        """Allowed AND set up for this person (so nobody sees commands that would only say 'not configured')."""
        return feature.name in self.services.access.allowed(pid) and feature.ready(self.services, pid)

    def _commands_for(self, pid: str) -> list[tuple[str, str, tuple[str, ...]]]:
        cmds = list(CORE_COMMANDS)
        for f in self.services.features:
            if self._ready(pid, f):
                cmds += [(c.name, c.description, c.usage) for c in f.commands]
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

    # ---- registration --------------------------------------------------------------------
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
        kb = [[InlineKeyboardButton(p["name"], callback_data=f"reg:{pid}")] for pid, p in self.cfg.profiles.items()]
        await update.effective_chat.send_message("Who are you?", reply_markup=InlineKeyboardMarkup(kb))

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

    async def on_document(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A file sent to the bot goes to the first feature (ready for this person) that takes its type."""
        import mimetypes
        if not (pid := await self._profile(update)):
            return
        doc = update.effective_message.document
        mime = doc.mime_type or mimetypes.guess_type(doc.file_name or "")[0] or ""
        for f in self.services.features:
            for kind, handler in f.documents:
                if kind == mime and self._ready(pid, f):
                    await handler(self, update, ctx)
                    return
        await self._reply(update, "I don't know what to do with that file.")

    async def help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        pid = self.db.profile_for(update.effective_chat.id)
        await self._reply(update, self._help(pid) if pid else "Send /start &lt;invite_code&gt; to register.")

    # ---- /features -----------------------------------------------------------------------
    def _features_view(self, pid: str) -> tuple[str, InlineKeyboardMarkup | None]:
        acc, cat = self.services.access, catalog()
        paused = acc.paused(pid)
        mine = [f.name for f in self.services.features if self._ready(pid, f)]
        if not mine:
            return "No features enabled for you.", None
        text = (f"<b>{html.escape(self.cfg.profiles[pid]['name'])}</b> — your features\n" + "\n".join(
            f"{'⏸️' if f in paused else '✅'} <b>{f}</b>: {html.escape(cat[f][0])}" for f in mine)
            + "\n\n<i>Tap to pause/resume scheduled messages (commands keep working).</i>")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{'▶️ Resume' if f in paused else '⏸️ Pause'} {f}",
                                                         callback_data=f"feat:{f}")] for f in mine])
        return text, kb

    async def features(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if pid := await self._profile(update):
            text, kb = self._features_view(pid)
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def toggle_feature(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        pid = self.db.profile_for(update.effective_chat.id)
        feature = q.data.split(":", 1)[1]
        if not pid or feature not in self.services.access.allowed(pid):
            await q.answer("Not available.", show_alert=True)
            return
        now_paused = feature not in self.services.access.paused(pid)
        self.services.access.set_paused(pid, feature, now_paused)
        log.info("%s %s %s", pid, "paused" if now_paused else "resumed", feature)
        await q.answer(f"{feature} {'paused' if now_paused else 'resumed'}")
        text, kb = self._features_view(pid)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._toggle(update, ctx, paused=True)

    async def resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._toggle(update, ctx, paused=False)

    async def _toggle(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, paused: bool) -> None:
        """Old /pause and /resume — still work, but point to /features."""
        if not (pid := await self._profile(update)):
            return
        feature = (ctx.args or [""])[0].lower()
        if feature in self.services.access.allowed(pid):
            self.services.access.set_paused(pid, feature, paused)
            log.info("%s %s %s", pid, "paused" if paused else "resumed", feature)
        text, kb = self._features_view(pid)
        await update.effective_message.reply_text(
            text + f"\n\n<i>Tip: /pause is now {CORE_ALIASES['pause']}.</i>", parse_mode=ParseMode.HTML, reply_markup=kb)

    # ---- admin ---------------------------------------------------------------------------
    async def status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Admin page about the app: alarms, every job's last run + next run, people, AI models.
        (The VM itself is /health.)"""
        if not (pid := await self._profile(update)):
            return
        if pid != self.cfg.primary_profile:
            await self._reply(update, "Only the primary user can see status.")
            return
        s = self.services
        lines = ["📊 <b>Nari status</b>", ""] + monitoring.format_alarms(s) + [""]
        nxt = {j.id: j.next_run_time for j in self.scheduler.get_jobs() if j.next_run_time}
        lines += monitoring.format_jobs(s, nxt) + [""]
        for f in s.features:
            if f.status_section and f.name in s.access.allowed(pid):
                lines += await f.status_section(s, pid) + [""]
        lines += ["<b>People</b>"]
        for p, prof in self.cfg.profiles.items():
            # Same rule as menus and /features: a feature counts only if it's allowed AND set up for them.
            reg = "registered" if self.db.chat_ids_for([p]) else "not registered"
            paused = s.access.paused(p)
            ready = [f.name for f in s.features if self._ready(p, f)]
            idle = [f for f in s.access.allowed(p) if f not in ready]
            on, off = [f for f in ready if f not in paused], [f for f in ready if f in paused]
            lines.append(f"• {html.escape(prof['name'])} ({reg}): " + (", ".join(on) or "nothing")
                         + (f" · paused: {', '.join(off)}" if off else "")
                         + (f" · allowed, not set up: {', '.join(idle)}" if idle else ""))
        lines += ["", "<b>AI models</b>"] + [f"• {html.escape(x)}" for x in s.llm_health.summary()]
        await self._reply(update, "\n".join(lines))

    async def run(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not (pid := await self._profile(update)):
            return
        if pid != self.cfg.primary_profile:
            await self._reply(update, "Only the primary user can run jobs.")
            return
        jobs = self.services.jobs()
        name = (ctx.args or [""])[0]
        if name not in jobs:
            await self._reply(update, "Jobs: " + " · ".join(jobs))
            return
        await self._reply(update, f"Running {name}…")
        await self.services.run_job(name)


def main() -> None:
    cfg = load_config()
    BotApp(cfg).app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
