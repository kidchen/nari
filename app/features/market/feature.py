"""Market briefs (morning + evening) from each person's own portfolio and watchlist."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime

from telegram.constants import ParseMode

from app import monitoring
from app.features.market import brief
from app.maintenance import TickerInfoCache
from app.notify import split_message
from app.registry import Command, Feature, Job

log = logging.getLogger(__name__)
BRIEF_CACHE_SECONDS = 30 * 60
LIST_LABELS = {"portfolio": "💼 Portfolio", "watch": "👀 Watchlist"}
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,9}$")


# ---- state + jobs --------------------------------------------------------------------------

def _state(s) -> dict:
    if "market" not in s.state:
        s.state["market"] = {"names": TickerInfoCache(s.db),   # symbol -> (name, quote type), 30 days in DB
                             "cache": {}}                        # "<pid>:<mode>" -> (ts, lists, text)
    return s.state["market"]


def seed_ticker_lists(s) -> None:
    for pid, lists in (s.cfg.settings["market"].get("seed_lists") or {}).items():
        if pid in s.cfg.profiles and not s.db.has_any_tickers(pid):
            for list_name in ("portfolio", "watch"):
                s.db.add_tickers(pid, list_name, [t.upper() for t in lists.get(list_name) or []])
            log.info("Seeded ticker lists for %s", pid)


def builder(s) -> brief.BriefBuilder:
    st = s.cfg.settings
    summarize = None
    if st["market"].get("llm", {}).get("enabled") and s.cfg.gemini_api_key:
        summarize = lambda p: s.llm("brief_models", p, json_output=True, retries=True)
    return brief.BriefBuilder(st, summarize, s.cfg.tz, _state(s)["names"])


def brief_for(s, profile: str, b: brief.BriefBuilder | None = None, use_cache: bool = False,
              mode: str = "morning") -> str:
    """Blocking: one person's brief (morning | evening). use_cache: reuse one built <30 min ago."""
    lists = (tuple(s.db.tickers(profile, "portfolio")), tuple(s.db.tickers(profile, "watch")))
    cache = _state(s)["cache"]
    hit = cache.get(f"{profile}:{mode}")
    if use_cache and hit and hit[1] == lists and time.time() - hit[0] < BRIEF_CACHE_SECONDS:
        log.info("Serving cached brief for %s", profile)
        return hit[2]
    text = (b or builder(s)).build(list(lists[0]), list(lists[1]), mode)
    cache[f"{profile}:{mode}"] = (time.time(), lists, text)
    return text


async def run(s, force: bool = False, mode: str = "morning") -> None:
    try:
        if not force and await asyncio.to_thread(brief.last_trading_day) != datetime.now(brief.NY).date():
            log.info("No US trading session today; skipping brief")
            monitoring.note("no US trading session today")
            return
        b = builder(s)                       # shared: each ticker/feed fetched once per run
        failed = []
        for pid in s.access.recipients("market"):
            if not s.db.chat_ids_for([pid]):
                continue
            try:                             # one person's failure must not cost the others their brief
                text = await asyncio.to_thread(brief_for, s, pid, b, False, mode)
                await s.n.send_text([pid], text)
            except Exception as e:  # noqa: BLE001
                log.warning("Brief for %s failed: %s", pid, e)
                failed.append(f"{pid}: {e}")
        if failed:
            raise RuntimeError("; ".join(failed))
    except Exception as e:  # noqa: BLE001
        await s.report_error(f"market {mode}", e)


async def run_evening(s, force: bool = False) -> None:
    await run(s, force, mode="evening")


# ---- commands ------------------------------------------------------------------------------

async def cmd_brief(bot, update, ctx) -> None:
    if not (pid := await bot._feature_profile(update, "market")):
        return
    args = [a.lower() for a in ctx.args or []]
    fresh = "fresh" in args
    # Default by time of day: before the 1pm PT close -> today's market; after -> close + look ahead.
    mode = "evening" if "evening" in args else "morning" if "morning" in args else (
        "evening" if datetime.now(brief.NY).hour >= 16 else "morning")
    await bot._reply(update, f"Building your {mode} brief… (about a minute)")
    try:
        text = await asyncio.to_thread(brief_for, bot.services, pid, None, not fresh, mode)
    except Exception as e:  # noqa: BLE001
        log.exception("On-demand brief failed")
        await bot._reply(update, f"Brief failed: {html.escape(str(e))[:300]}")
        return
    for chunk in split_message(text):
        await update.effective_chat.send_message(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def _edit_list(bot, update, ctx, list_name: str) -> None:
    if not (pid := await bot._feature_profile(update, "market")):
        return
    db = bot.services.db
    cmd, label = f"/{list_name}", LIST_LABELS[list_name]
    args = [a.strip(",").upper() for a in (ctx.args or []) if a.strip(",")]
    action, symbols = (args[0].lower(), args[1:]) if args else ("show", [])
    if action in ("add", "del", "rm", "remove") and not symbols:
        await bot._reply(update, f"Usage: {cmd} {action} TICKER [TICKER ...]")
        return
    if action == "add":
        bad = [t for t in symbols if not SYMBOL_RE.match(t)]
        valid, invalid = await asyncio.to_thread(brief.validate_symbols, [t for t in symbols if t not in bad])
        added = db.add_tickers(pid, list_name, valid)
        msg = f"Added to {label}: {', '.join(added) or 'nothing new'}"
        if bad + invalid:
            msg += f"\nNot found on Yahoo: {html.escape(', '.join(bad + invalid))}"
    elif action in ("del", "rm", "remove"):
        removed = db.remove_tickers(pid, list_name, symbols)
        msg = f"Removed from {label}: {', '.join(removed) or 'none of those were on it'}"
    elif action == "show":
        msg = ""
    else:
        await bot._reply(update, f"Usage: {cmd} | {cmd} add TICKER ... | {cmd} del TICKER ...")
        return
    current = db.tickers(pid, list_name)
    msg = (msg + "\n\n" if msg else "") + f"<b>{label}</b> ({len(current)}): " + (", ".join(current) or "empty")
    await bot._reply(update, msg)


async def cmd_portfolio(bot, update, ctx) -> None:
    await _edit_list(bot, update, ctx, "portfolio")


async def cmd_watch(bot, update, ctx) -> None:
    await _edit_list(bot, update, ctx, "watch")


def _on_startup(s) -> None:
    _state(s)
    seed_ticker_lists(s)


def _sched(key: str):
    return lambda st: st["market"][key] if st.get("market", {}).get("enabled") else None


FEATURE = Feature(
    name="market",
    description="Morning + evening market briefs (/brief, /portfolio, /watch)",
    commands=(
        Command("brief", "Market brief now: today before the close, close + look-ahead after", cmd_brief,
                ("/brief morning · /brief evening — pick one", "/brief fresh — skip the 30-min cache")),
        Command("portfolio", "Your portfolio", cmd_portfolio, ("/portfolio add IBM KO — add", "/portfolio del KO — remove")),
        Command("watch", "Your watchlist", cmd_watch, ("/watch add XOM — add", "/watch del XOM — remove")),
    ),
    jobs=(Job("market", run, _sched("schedule")), Job("evening", run_evening, _sched("evening_schedule"))),
    on_startup=_on_startup,
)
