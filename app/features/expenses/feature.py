"""Expenses (per person): card spending from statement PDFs, with weekly to year-end reports (alert emails: planned).

Statement flow: a PDF sent to the bot is saved on the server (owner-only file), parsed, and checked
against the statement's own balances. The person sees a preview with Import / Cancel; the PDF is
deleted either way. Unparseable PDFs are kept (30 days, nightly cleanup) so the parser can be fixed.
Statements never leave the server; Gemini gets only merchant names (categories) and category totals
(report takeaways). Logs carry counts only, never merchants or amounts.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import monitoring
from app.features.expenses import alerts, categories, reports, statements, store
from app.registry import Command, Feature, Job, MailQuery

log = logging.getLogger(__name__)
MAX_BYTES = 20 * 1024 * 1024          # Telegram's bot download limit


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def money(c: int) -> str:
    return f"{'-' if c < 0 else ''}${abs(c) / 100:,.2f}"


def inbox(s, pid: str) -> Path:
    return s.cfg.db_path.parent / "statements" / "inbox" / pid


def save_statement(s, pid: str, name: str, data: bytes, now: datetime | None = None) -> Path:
    """Owner-only file under data/statements/inbox/<pid>/, timestamped, with a filesystem-safe name."""
    d = inbox(s, pid)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = re.sub(r"\.pdf$", "", name or "", flags=re.I)                     # a name, never a path
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:60] or "statement"
    path = d / f"{(now or datetime.now(s.cfg.tz)):%Y%m%d-%H%M%S}-{stem}.pdf"
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def card_label(s, pid: str, st: statements.Statement) -> str:
    """The card's name from the cards config if its `last4` matches, else '<Bank> ··1234'."""
    for c in s.cfg.cards.values():
        if c.get("owner") == pid and st.last4 in [str(x) for x in (c.get("last4") or [])]:
            return f"{c['name']} ··{st.last4}"
    return f"{statements.BANKS[st.bank]['name']} ··{st.last4}"


def preview(s, pid: str, st: statements.Statement) -> str:
    span = f"{st.start:%b %d} – {st.end:%b %d, %Y}" if st.start else f"to {st.end:%b %d, %Y}"
    n = lambda *k: sum(1 for t in st.txns if t.kind in k)  # noqa: E731
    parts = [f"Purchases {money(st.total('purchase'))} ({n('purchase')})"]
    for label, kinds in (("Refunds", ("credit",)), ("Fees", ("fee",)), ("Interest", ("interest",)),
                         ("Cash advances", ("cash_advance",)), ("Transfers", ("balance_transfer",)), ("Payments", ("payment",))):
        if n(*kinds):
            parts.append(f"{label} {money(st.total(*kinds))}")
    lines = [f"🧾 <b>{_esc(card_label(s, pid, st))}</b> · {span}", " · ".join(parts),
             f"✅ Adds up: previous {money(st.previous_cents)} + activity {money(st.activity_cents)} "
             f"= new balance {money(st.new_cents)}"]
    if m := store.preview_matches(s.db, pid, st):
        lines.append(f"Alert emails for this period: {m['confirmed']} confirmed, {m['tip']} tip-adjusted, "
                     f"{m['dropped']} not on the statement (dropped). The statement replaces them.")
    if when := store.imported_at(s.db, store.statement_id(pid, st)):
        lines.append(f"<i>Already imported {when[:10]}; importing again replaces it.</i>")
    return "\n".join(lines)


async def offer_import(bot, update, pid: str, path: Path, name: str) -> None:
    """Parse a saved PDF and reply with the preview + buttons, or why it can't be imported."""
    s = bot.services
    try:
        st = await asyncio.to_thread(statements.parse_pdf, str(path))
    except Exception as e:  # noqa: BLE001 — a broken PDF must not crash the handler
        log.warning("Statement parse failed for %s: %s", pid, type(e).__name__)
        st = statements.Statement("?", problems=["The PDF couldn't be read."])
    log.info("Statement %s for %s: bank=%s ok=%s txns=%d", path.name, pid, st.bank, st.ok, len(st.txns))
    if not st.ok:
        await bot._reply(update, f"⚠️ Couldn't import <b>{_esc(name)}</b>:\n" + "\n".join(f"• {_esc(p)}" for p in st.problems)
                         + "\n<i>The file is kept on the server for 30 days so the parser can be fixed.</i>")
        return
    token = secrets.token_hex(6)
    s.db.set_kv(f"exp:pending:{token}", f"{pid}|{path}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Import", callback_data=f"exp:imp:{token}"),
                                InlineKeyboardButton("✖️ Cancel", callback_data=f"exp:no:{token}")]])
    await update.effective_message.reply_text(preview(s, pid, st), parse_mode="HTML", reply_markup=kb)


async def on_document(bot, update, ctx) -> None:
    if not (pid := await bot._feature_profile(update, "expenses")):
        return
    doc = update.effective_message.document
    if doc.file_size and doc.file_size > MAX_BYTES:
        await bot._reply(update, "That file is over 20 MB, which is more than Telegram lets bots download.")
        return
    data = bytes(await (await doc.get_file()).download_as_bytearray())
    if not data.startswith(b"%PDF"):
        await bot._reply(update, "That doesn't look like a PDF. Send the statement PDF from your bank.")
        return
    path = save_statement(bot.services, pid, doc.file_name or "", data)
    await offer_import(bot, update, pid, path, doc.file_name or path.name)


async def on_button(bot, update, ctx) -> None:
    q = update.callback_query
    s = bot.services
    _, action, token = q.data.split(":", 2)
    raw = s.db.get_kv(f"exp:pending:{token}")
    pid = s.db.profile_for(update.effective_chat.id)
    if not raw or raw.split("|", 1)[0] != pid:
        await q.answer("That statement is no longer pending.", show_alert=True)
        return
    path = Path(raw.split("|", 1)[1])
    with s.db.tx() as c:
        c.execute("DELETE FROM kv WHERE k=?", (f"exp:pending:{token}",))
    if action == "imp":
        st = await asyncio.to_thread(statements.parse_pdf, str(path))
        if not st.ok:                                     # file changed or parser changed since the preview
            await q.answer("It no longer checks out; nothing imported.", show_alert=True)
            return
        store.import_statement(s.db, pid, st)
        counts = await asyncio.to_thread(store.categorize, s.db, _llm(s), pid)
        log.info("Imported statement for %s: %d txns, categories %s", pid, len(st.txns), counts)
        done = f"\n\n✅ Imported {len(st.txns)} transactions. /spend shows them."
    else:
        done = "\n\n✖️ Cancelled; nothing imported."
    path.unlink(missing_ok=True)                          # the PDF is never kept after a decision
    await q.answer()
    await q.edit_message_text(q.message.text_html + done, parse_mode="HTML")


def _llm(s):
    return (lambda p: s.llm("brief_models", p, json_output=True)) if s.cfg.gemini_api_key else None


def _has_data(s, pid: str) -> bool:
    return bool(s.db.rows("SELECT 1 FROM expense_txns WHERE owner=? LIMIT 1", (pid,)))


async def send_report(s, pid: str, kind: str, current: bool = False, to: str | None = None) -> reports.Report:
    """Build `pid`'s report and send it to `to` (default: pid). Chart first (if any), then the text."""
    llm = _llm(s) if kind != "week" else None
    rep = await asyncio.to_thread(reports.build, s.db, pid, kind, s.today(), s.cfg.cards, llm, current)
    if rep.chart:
        await s.n.send_photo([to or pid], rep.chart, rep.caption)
    await s.n.send_text([to or pid], rep.text)
    return rep


async def run_report(s, kind: str, force: bool = False) -> None:
    """Scheduled: everyone who gets expenses, is registered and has imported anything. Silent otherwise."""
    for pid in s.access.recipients("expenses"):
        if not s.db.chat_ids_for([pid]) or not _has_data(s, pid):
            continue
        try:
            rep = await send_report(s, pid, kind)
            monitoring.note(f"{pid}: {kind}{' (no activity)' if rep.empty else ''}")
        except Exception as e:  # noqa: BLE001
            await s.report_error(f"spending {kind} report for {pid}", e)


def _report_job(kind: str):
    async def run(s, force: bool = False) -> None:
        await run_report(s, kind, force)
    run.__name__ = f"run_{kind}_report"
    return run


# ---- statement reminder (statements are optional; alerts are the main source) -----------------------

REMIND_EVERY_DAYS = 90        # after the last statement import (or a Skip)
RENAG_DAYS = 14               # no answer: remind again after this long


def last_import(s, pid: str) -> str | None:
    return s.db.rows("SELECT max(imported) i FROM expense_statements WHERE owner=?", (pid,))[0]["i"]


def reminder_due(s, pid: str, today) -> bool:
    if until := s.db.get_kv(f"exp:remind:{pid}"):
        if today.isoformat() < until:
            return False
    start = last_import(s, pid) or s.db.registered_at(pid) or ""
    return bool(start) and (today - datetime.fromisoformat(start).date()).days >= REMIND_EVERY_DAYS


async def run_statement_reminder(s, force: bool = False) -> None:
    """Daily: once 3 months passed since someone's last statement import, one reminder listing the cards
    in use whose statements are older; [Skip 3 months] (or any import) resets it."""
    today = s.today()
    for pid in s.access.recipients("expenses"):
        if not s.db.chat_ids_for([pid]) or not _has_data(s, pid) or not (force or reminder_due(s, pid, today)):
            continue
        cards = reports.stale_statements(s.db, pid, today)
        if not cards:
            continue
        s.db.set_kv(f"exp:remind:{pid}", (today + timedelta(days=RENAG_DAYS)).isoformat())
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Skip 3 months", callback_data=f"exp:skip:{pid}")]])
        await s.send_isolated(pid, "🧾 <b>Statements, optional</b>: none imported for 3 months for "
                              f"{_esc(', '.join(cards))}. Sending them corrects tips and adds refunds; "
                              "card alerts keep reports going either way.", kb)
        monitoring.note(f"{pid}: statement reminder")


async def on_skip_button(bot, update, ctx) -> None:
    q = update.callback_query
    s = bot.services
    pid = s.db.profile_for(update.effective_chat.id)
    if q.data.split(":", 2)[2] != pid:
        await q.answer("Not yours.", show_alert=True)
        return
    until = s.today() + timedelta(days=REMIND_EVERY_DAYS)
    s.db.set_kv(f"exp:remind:{pid}", until.isoformat())
    await q.answer()
    await q.edit_message_text(q.message.text_html + f"\n\n⏭️ Skipped. Next reminder around {until:%b %d}.", parse_mode="HTML")


def _sched(key: str, default: dict):
    return lambda st: (st.get("expenses") or {}).get(key, default) if (st.get("expenses") or {}).get("reports", True) else None


# ---- bank alert emails (between statements) --------------------------------------------------------

def alert_owner(s, alert: alerts.Alert, sender: str) -> str | None:
    """The card's owner (cards config `last4`); else whoever forwarded it (profiles.<id>.email_from);
    else the only person with expenses. Ambiguous -> None (skipped, logged)."""
    for c in s.cfg.cards.values():
        if alert.last4 in [str(x) for x in (c.get("last4") or [])] and c.get("owner") in s.cfg.profiles:
            return c["owner"]
    for pid, p in s.cfg.profiles.items():
        if any(a and a.lower() in sender.lower() for a in (p.get("email_from") or [])):
            return pid
    people = [pid for pid in s.cfg.profiles if "expenses" in s.access.allowed(pid)]
    return people[0] if len(people) == 1 else None


async def handle_mail(s, mails, force: bool = False) -> None:
    """New alert emails -> charges (silent; they show up in /spend and the reports)."""
    counts: dict[str, int] = {}
    try:
        for m in mails:
            alert = alerts.parse(m.html)
            pid = alert and alert_owner(s, alert, m.sender)
            if not alert or not pid or "expenses" not in s.access.allowed(pid):
                result = "not a charge" if not alert else "no owner"
            else:
                result = store.add_alert(s.db, pid, alert, m.message_id)
            counts[result] = counts.get(result, 0) + 1
        if counts.get("added"):
            await asyncio.to_thread(store.categorize, s.db, _llm(s))
        s.db.mark_emails_processed([m.message_id for m in mails])
        if mails:
            log.info("Card alerts: %s", counts)
            monitoring.note("card alerts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    except Exception as e:  # noqa: BLE001
        await s.report_error("card alerts", e)


async def run_alert_check(s, force: bool = False) -> None:
    """/run spendmail: just this feature's Gmail search (the scheduled `mail` job covers all features)."""
    await s.mail_check(force, only=["expenses"])


def _alerts_cfg(st: dict) -> dict:
    return {"lookback_days": 14, "mailboxes": ["INBOX", "[Gmail]/Spam"], **((st.get("expenses") or {}).get("alerts") or {})}


async def run_categorize(s, force: bool = False) -> None:
    """Daily: categorize anything still uncategorized (e.g. Gemini was down at import time)."""
    counts = await asyncio.to_thread(store.categorize, s.db, _llm(s))
    monitoring.note(", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to categorize")


def summary(s, pid: str, days: int = 30) -> str:
    end = s.today()
    start = end - timedelta(days=days - 1)
    rows = store.spending(s.db, pid, start, end)
    if not rows:
        last = store.statements(s.db, pid)
        hint = f" Latest statement ends {last[0]['end']}." if last else " Send a statement PDF to start."
        return f"💳 No spending recorded for {start:%b %d}–{end:%b %d}.{hint}"
    total = sum(r["amount"] for r in rows)
    by_cat, by_merchant = defaultdict(int), defaultdict(int)
    for r in rows:
        by_cat[r["category"] or "Uncategorized"] += r["amount"]
        if r["kind"] == "purchase":
            by_merchant[r["merchant"] or categories.merchant_key(r["description"])] += r["amount"]
    lines = [f"💳 <b>Spending</b> · {start:%b %d} – {end:%b %d} · <b>{money(total)}</b> ({len(rows)} items)", "",
             "<b>By category</b>"]
    lines += [f"• {_esc(c)}: {money(v)}" for c, v in sorted(by_cat.items(), key=lambda kv: -kv[1])]
    lines += ["", "<b>Top merchants</b>"]
    lines += [f"• {_esc(categories.display_name(s.db, pid, m))}: {money(v)}"
              for m, v in sorted(by_merchant.items(), key=lambda kv: -kv[1])[:5]]
    lines += ["", "<i>Wrong category? /spend fix &lt;merchant&gt;</i>"]
    return "\n".join(lines)


async def cmd_spend(bot, update, ctx) -> None:
    """/spend [days] — spending summary. /spend inbox — re-offer saved, not yet imported PDFs."""
    if not (pid := await bot._feature_profile(update, "expenses")):
        return
    args = [a.lower() for a in ctx.args or []]
    s = bot.services
    if args[:1] == ["fix"]:
        await _fix_start(bot, update, pid, " ".join((ctx.args or [])[1:]).strip())
        return
    if args[:1] == ["name"]:
        await _rename(bot, update, pid, " ".join((ctx.args or [])[1:]))
        return
    if args[:1] == ["report"]:
        await _report_cmd(bot, update, pid, args[1:])
        return
    if args[:1] == ["subs"]:
        subs = reports.subscriptions(s.db, pid, s.today())
        text = "\n".join(reports._sub_lines(s.db, pid, subs, reports.period("month", s.today(), True))) or \
            "No subscriptions detected yet (needs 3 monthly or 2 yearly charges from the same merchant)."
        await bot._reply(update, text)
        return
    if s.db.rows("SELECT 1 FROM expense_txns WHERE owner=? AND category='' AND kind!='payment' LIMIT 1", (pid,)):
        await asyncio.to_thread(store.categorize, s.db, _llm(s), pid)
    if args[:1] == ["inbox"]:
        files = sorted(inbox(bot.services, pid).glob("*.pdf")) if inbox(bot.services, pid).exists() else []
        if not files:
            await bot._reply(update, "No saved statements waiting.")
        for f in files:
            await offer_import(bot, update, pid, f, f.name.split("-", 2)[-1])
        return
    days = int(args[0]) if args and args[0].isdigit() and 0 < int(args[0]) <= 400 else 30
    await bot._reply(update, summary(bot.services, pid, days))


async def _report_cmd(bot, update, pid: str, args: list[str]) -> None:
    """/spend report <week|month|quarter|year> [now] [person] — 'now' = the current period so far;
    a person's name is admin-only."""
    s = bot.services
    kind = next((a for a in args if a in reports.KINDS), "month")
    who = next((a for a in args if a in s.cfg.profiles), pid)
    if who != pid and pid != s.cfg.primary_profile:
        await bot._reply(update, "You can only see your own reports.")
        return
    if who != pid and "expenses" not in s.access.allowed(who):
        await bot._reply(update, f"{_esc(who)} doesn't have expenses enabled.")
        return
    chat_pid = s.db.profile_for(update.effective_chat.id)
    await send_report(s, who, kind, current="now" in args, to=chat_pid)


async def _rename(bot, update, pid: str, text: str) -> None:
    """/spend name <merchant> = <display name>"""
    s = bot.services
    if "=" not in text:
        await bot._reply(update, "Usage: /spend name &lt;merchant&gt; = &lt;new name&gt;, e.g. /spend name blue door = Blue Door Cafe")
        return
    what, new = (x.strip() for x in text.split("=", 1))
    found = store.merchants_like(s.db, pid, what) if what and new else []
    if len(found) != 1:
        msg = "No merchant matching that." if not found else \
            "Several merchants match: " + ", ".join(_esc(m["merchant"]) for m in found) + ". Be more specific."
        await bot._reply(update, msg)
        return
    categories.rename(s.db, pid, found[0]["merchant"], new)
    await bot._reply(update, f"✅ {_esc(found[0]['merchant'])} now shows as <b>{_esc(new[:40])}</b>.")


# ---- /spend fix: correct a merchant's category (remembered for the future) -------------------------

def _cat_buttons(token: str) -> InlineKeyboardMarkup:
    cats = categories.CATEGORIES
    rows = [[InlineKeyboardButton(c, callback_data=f"exp:fc:{token}:{i}") for i, c in enumerate(cats) if i // 3 == r]
            for r in range((len(cats) + 2) // 3)]
    return InlineKeyboardMarkup(rows)


def _token(s, pid: str, key: str) -> str:
    token = secrets.token_hex(6)
    s.db.set_kv(f"exp:fix:{token}", f"{pid}|{key}")
    return token


async def _fix_start(bot, update, pid: str, text: str) -> None:
    s = bot.services
    if not text:
        await bot._reply(update, "Usage: /spend fix &lt;merchant&gt;, e.g. /spend fix costco")
        return
    found = store.merchants_like(s.db, pid, text)
    if not found:
        await bot._reply(update, f"No merchant matching “{_esc(text)}” in your transactions.")
        return
    if len(found) == 1:
        m = found[0]
        await update.effective_message.reply_text(
            f"<b>{_esc(m['merchant'])}</b> ({m['n']}×, {money(m['total'])}) is <b>{_esc(m['category'] or 'Uncategorized')}</b>. "
            "Pick the right category:", parse_mode="HTML", reply_markup=_cat_buttons(_token(s, pid, m["merchant"])))
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{m['merchant']} · {m['category'] or '?'} · {money(m['total'])}",
                                                     callback_data=f"exp:fm:{_token(s, pid, m['merchant'])}")] for m in found])
    await update.effective_message.reply_text("Which merchant?", reply_markup=kb)


async def on_fix_button(bot, update, ctx) -> None:
    q = update.callback_query
    s = bot.services
    parts = q.data.split(":")
    raw = s.db.get_kv(f"exp:fix:{parts[2]}")
    pid = s.db.profile_for(update.effective_chat.id)
    if not raw or raw.split("|", 1)[0] != pid:
        await q.answer("That choice expired. Send /spend fix again.", show_alert=True)
        return
    key = raw.split("|", 1)[1]
    await q.answer()
    if parts[1] == "fm":                                  # merchant picked -> show categories
        await q.edit_message_text(f"<b>{_esc(key)}</b>: pick the right category:", parse_mode="HTML",
                                  reply_markup=_cat_buttons(parts[2]))
        return
    cat = categories.CATEGORIES[int(parts[3])]
    n = store.set_category(s.db, pid, key, cat)
    with s.db.tx() as c:
        c.execute("DELETE FROM kv WHERE k=?", (f"exp:fix:{parts[2]}",))
    await q.edit_message_text(f"✅ <b>{_esc(key)}</b> → {_esc(cat)} ({n} transaction(s) updated). "
                              "Future charges there use it too.", parse_mode="HTML")


FEATURE = Feature(
    name="expenses",
    description="Card spending from statement PDFs, with weekly to year-end reports",
    commands=(Command("spend", "Your card spending", cmd_spend,
                      ("/spend 90 — last 90 days · /spend subs — subscriptions",
                       "/spend report week|month|quarter|year [now]",
                       "/spend fix costco — correct a category · /spend name costco = Costco",
                       "/spend inbox — import saved statements · send a statement PDF to import it")),),
    callbacks=((r"^exp:(imp|no):", on_button), (r"^exp:(fm|fc):", on_fix_button), (r"^exp:skip:", on_skip_button)),
    mail=MailQuery(criteria=lambda st: alerts.imap_criteria(alerts.SUBJECTS),
                   lookback_days=lambda st: _alerts_cfg(st)["lookback_days"],
                   mailboxes=lambda st: tuple(_alerts_cfg(st)["mailboxes"]),
                   handle=handle_mail),
    jobs=(Job("spendcat", run_categorize, lambda st: {"hour": 4, "minute": 15}),
          Job("spendmail", run_alert_check),
          Job("spendremind", run_statement_reminder, _sched("statement_reminder_schedule", {"hour": 9, "minute": 30})),
          Job("spendweek", _report_job("week"), _sched("weekly_schedule", {"day_of_week": "sat", "hour": 8, "minute": 0})),
          Job("spendmonth", _report_job("month"), _sched("monthly_schedule", {"day": 1, "hour": 8, "minute": 5})),
          Job("spendquarter", _report_job("quarter"),
              _sched("quarterly_schedule", {"month": "1,4,7,10", "day": 1, "hour": 8, "minute": 10})),
          Job("spendyear", _report_job("year"), _sched("yearly_schedule", {"month": 1, "day": 2, "hour": 9, "minute": 0}))),
    extra={"settings_key": "expenses"},
    documents=(("application/pdf", on_document),),
)
