"""Quarterly 5% categories (shared, from issuer emails) + per-person activation status and reminders."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import date

from telegram.constants import ParseMode

from app.features.cards import cards, rotating
from app.notify import buttons
from app.registry import Alias, Command, Feature, Job, MailQuery

log = logging.getLogger(__name__)


# ---- helpers -------------------------------------------------------------------------------

def activation_markup(s, pid: str, today: date):
    return buttons(cards.activation_buttons(s.cfg.cards, pid, today, s.db))


def card_owners(s, issuer: str) -> list[str]:
    return sorted({c["owner"] for c in s.cfg.cards.values()
                   if (c.get("rotating") or {}).get("issuer") == issuer and s.access.receives(c["owner"], "cards")})


def email_owner(s, sender: str, owners_by_addr: dict[str, str]) -> str | None:
    """Whose 5% email is this? The forwarding address decides; mail straight from a bank is attributed
    only if exactly one person has the bot's card feature (otherwise it's ambiguous)."""
    for addr, pid in owners_by_addr.items():
        if addr in sender:
            return pid
    holders = [pid for pid in s.cfg.profiles if "cards" in s.access.allowed(pid)
               and any(c.get("owner") == pid for c in s.cfg.cards.values())]
    return holders[0] if len(holders) == 1 else None


def ending_owner(s, html: str, issuer: str) -> str | None:
    """Whose card is the email about? Banks print the ending ("account number ending with 1234")."""
    for digits in re.findall(r"ending (?:in|with)\s*(\d{4,5})", rotating.email_text(html), re.I):
        if c := cards.find_card(s.cfg.cards, issuer, digits):
            return c.get("owner")
    return None


def collect_card_emails(s, mails) -> list[rotating.QuarterInfo]:
    """Blocking: parse 5%-category emails, merge into the DB. Returns quarters that are new or changed."""
    cfg, em = s.cfg, s.cfg.settings["cards"]["email"]
    owners_by_addr = {a.lower(): pid for pid, p in cfg.profiles.items() for a in p.get("email_from", [])}
    allowed = [x.lower() for x in em.get("senders", []) + list(owners_by_addr)]
    llm_fn = (lambda p: s.llm("extraction_models", p, json_output=True)) if cfg.gemini_api_key else None
    changed: list[rotating.QuarterInfo] = []
    for m in mails:
        sender = m.sender.lower()
        if not any(a in sender for a in allowed):
            log.info("Ignoring 5%% email from unlisted sender %r", m.sender)
            continue
        for q in rotating.parse_email(m.sender, m.subject, m.html, llm_fn).quarters:
            owner = ending_owner(s, m.html, q.issuer) or email_owner(s, sender, owners_by_addr)
            old = cards.load_email_quarter(s.db, q.issuer, q.quarter)
            merged = rotating.merge(old, q)
            cards.save_email_quarter(s.db, merged)                  # shared: public categories
            status_changed = False
            if owner:                                                # personal: only the owner's status
                status_changed = cards.save_owner_status(s.db, owner, q)
            elif q.activated is not None or q.activate_url:
                log.info("5%% email from %r has no known owner; activation status not recorded", m.sender)
            if old is None or old.categories != merged.categories or status_changed:
                changed.append(merged)
            log.info("%s %s: %s (owner=%s, activated=%s, preview=%s)", q.issuer, q.quarter, merged.categories,
                     owner, q.activated, merged.preview)
    return changed


def migrate_card_status(s) -> int:
    """One-time: older versions stored activation status + link inside the shared per-issuer record.
    Move them to the card owner (only when exactly one person holds that issuer's card)."""
    moved = 0
    for _key, v in s.db.kv_like("rotq:%"):
        data = json.loads(v)
        if data.get("activated") is None and not data.get("activate_url"):
            continue
        info = rotating.QuarterInfo(**data)
        owners = {c["owner"] for c in s.cfg.cards.values() if (c.get("rotating") or {}).get("issuer") == info.issuer}
        if len(owners) == 1:
            cards.save_owner_status(s.db, owners.pop(), info)
        cards.save_email_quarter(s.db, info)          # rewrite shared record without personal fields
        moved += 1
    return moved


# ---- jobs ----------------------------------------------------------------------------------

async def handle_mail(s, mails, force: bool = False) -> None:
    """Issuers' 5% emails -> tell card owners when a quarter's categories appear/change."""
    try:
        changed = await asyncio.to_thread(collect_card_emails, s, mails)
        if changed or force:
            today = s.today()
            owners = {o for q in changed for o in card_owners(s, q.issuer)} or (
                set(s.access.recipients("cards")) if force else set())
            for pid in sorted(owners):
                if msg := cards.quarterly_message(s.cfg.cards, pid, s.cfg.profiles[pid]["name"], today, s.db):
                    head = "📬 <b>New 5% info from your card emails</b>\n\n" if changed else ""
                    await s.send_isolated(pid, head + msg, activation_markup(s, pid, today))
        s.db.mark_emails_processed([m.message_id for m in mails])
    except Exception as e:  # noqa: BLE001
        await s.report_error("card_emails", e)


async def run_check(s, force: bool = False) -> None:
    await s.mail_check(force, only=["cards"])


async def run_quarterly(s, force: bool = False) -> None:
    """1st of each quarter: this quarter's categories + activation status, per person."""
    try:
        today = s.today()
        for pid in s.access.recipients("cards"):
            if msg := cards.quarterly_message(s.cfg.cards, pid, s.cfg.profiles[pid]["name"], today, s.db):
                await s.send_isolated(pid, msg, activation_markup(s, pid, today))
    except Exception as e:  # noqa: BLE001
        await s.report_error("quarterly_cards", e)


async def run_activation_reminder(s, force: bool = False) -> None:
    """Mid-month nudge, only for cards whose quarter isn't confirmed activated."""
    try:
        today = s.today()
        for pid in s.access.recipients("cards"):
            if pending := cards.needs_activation(s.cfg.cards, pid, today, s.db):
                msg = cards.quarterly_message(s.cfg.cards, pid, s.cfg.profiles[pid]["name"], today, s.db)
                await s.send_isolated(pid, "⏰ <b>Reminder:</b> activate 5% on " + html.escape(", ".join(pending))
                                      + "\n\n" + (msg or ""), activation_markup(s, pid, today))
    except Exception as e:  # noqa: BLE001
        await s.report_error("activation_reminder", e)


# ---- commands ------------------------------------------------------------------------------

async def cmd_cards(bot, update, ctx) -> None:
    """/cards · /cards <category> (best card) · /cards fix <card_id> A, B (manual correction)."""
    args = ctx.args or []
    if args and args[0].lower() == "fix":
        ctx.args = args[1:]
        return await cmd_fix(bot, update, ctx)
    if args:
        return await cmd_best(bot, update, ctx)
    if pid := await bot._feature_profile(update, "cards"):
        s, today = bot.services, bot._today()
        msg = cards.quarterly_message(s.cfg.cards, pid, s.cfg.profiles[pid]["name"], today, s.db)
        await update.effective_message.reply_text(
            msg or "No rotating 5% cards configured for you (config/cards.local.yaml).", parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=activation_markup(s, pid, today) if msg else None)


async def cmd_best(bot, update, ctx) -> None:
    if not (pid := await bot._feature_profile(update, "cards")):
        return
    if not ctx.args:
        await bot._reply(update, "Usage: /cards &lt;category&gt;, e.g. /cards groceries")
        return
    cat = " ".join(ctx.args)
    ranked = cards.best_card(bot.services.cfg.cards, pid, cat, bot._today(), bot.services.db)
    if not ranked:
        await bot._reply(update, "No cards configured for you.")
        return
    lines = [f"Best for <b>{html.escape(cat)}</b>:"]
    lines += [f"{i}. {html.escape(name)} — {rate:g}% ({reason})" for i, (rate, name, reason) in enumerate(ranked, 1)]
    await bot._reply(update, "\n".join(lines))


async def cmd_fix(bot, update, ctx) -> None:
    if not (pid := await bot._feature_profile(update, "cards")):
        return
    s, args = bot.services, ctx.args or []
    mine = {cid: c for cid, c in cards.cards_for(s.cfg.cards, pid).items() if c.get("rotating")}
    if len(args) < 2 or args[0] not in mine:
        await bot._reply(update, "Usage: /cards fix &lt;card_id&gt; Category A, Category B\nYour rotating cards: "
                         + (", ".join(mine) or "none"))
        return
    cats = [c.strip() for c in " ".join(args[1:]).split(",") if c.strip()]
    qkey = cards.quarter_key(bot._today())
    s.db.set_kv(cards.override_key(args[0], qkey), json.dumps(cats))
    log.info("%s set %s %s categories: %s", pid, args[0], qkey, cats)
    await bot._reply(update, f"Saved {qkey} categories for {html.escape(mine[args[0]]['name'])}: "
                     + html.escape(", ".join(cats)))


async def on_activation_button(bot, update, ctx) -> None:
    """'✅ I activated <card>' button, for issuers that never email a confirmation (e.g. Discover)."""
    q = update.callback_query
    s = bot.services
    pid = s.db.profile_for(update.effective_chat.id)
    _, card_id, qkey = q.data.split(":", 2)
    card = s.cfg.cards.get(card_id)
    if not pid or not card or card.get("owner") != pid:
        await q.answer("That isn't one of your cards.", show_alert=True)
        return
    cards.mark_activated(s.db, card_id, qkey)
    log.info("%s confirmed %s activated for %s", pid, card_id, qkey)
    await q.answer(f"Marked {card['name']} as activated for {qkey}")
    today = bot._today()
    msg = cards.quarterly_message(s.cfg.cards, pid, s.cfg.profiles[pid]["name"], today, s.db)
    try:  # refresh the message in place so its status and buttons update
        await q.edit_message_text(msg or "", parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                  reply_markup=activation_markup(s, pid, today))
    except Exception:  # noqa: BLE001 — e.g. the button was on a reminder with extra text
        await q.edit_message_reply_markup(activation_markup(s, pid, today))


def _on_startup(s) -> None:
    log.info("Card status moved to owners: %d", migrate_card_status(s))


def _sched(key: str):
    return lambda st: st["cards"][key] if st.get("cards", {}).get("enabled") else None


FEATURE = Feature(
    name="cards",
    description="Quarterly 5% categories + activation reminders",
    commands=(Command("cards", "This quarter's 5% categories and activation", cmd_cards,
                      ("/cards dining — which of your cards to use",
                       "/cards fix discover_it Gas, Transit — correct categories (rare)")),),
    aliases=(Alias("best", cmd_best, "/cards <category>"), Alias("setcats", cmd_fix, "/cards fix <card> A, B")),
    callbacks=((r"^act:", on_activation_button),),
    jobs=(Job("cards", run_quarterly, _sched("quarterly_schedule")),
          Job("cardmail", run_check),
          Job("activation", run_activation_reminder, _sched("activation_reminder_schedule"))),
    mail=MailQuery(criteria=lambda st: ["SUBJECT", f'"{st["cards"]["email"]["subject"]}"'],
                   lookback_days=lambda st: st["cards"]["email"]["lookback_days"],
                   mailboxes=lambda st: tuple(st["cards"]["email"]["mailboxes"]),
                   handle=handle_mail),
    ready=lambda s, pid: bool(cards.cards_for(s.cfg.cards, pid)),
    on_startup=_on_startup,
)
