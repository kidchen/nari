"""bioRxiv digest per person: their keywords (+ context terms), their schedule, deduped per person."""
from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import UTC, date, datetime, timedelta

from app import monitoring
from app.features.biorxiv import biorxiv
from app.registry import Alias, Command, Feature, Job

log = logging.getLogger(__name__)


# ---- per-person terms (edited in the bot, seeded from config) ------------------------------

def terms(s, pid: str, kind: str = "keywords") -> list[str]:
    """This person's `keywords` or `context` terms: edited in the bot (/papers) or, until then, config."""
    if raw := s.db.get_kv(f"biorxiv:{kind}:{pid}"):
        return json.loads(raw)
    return list((s.cfg.profiles[pid].get("biorxiv") or {}).get(kind) or [])


def set_terms(s, pid: str, values: list[str] | None, kind: str = "keywords") -> None:
    """None = back to the configured list."""
    if values is None:
        with s.db.tx() as c:
            c.execute("DELETE FROM kv WHERE k=?", (f"biorxiv:{kind}:{pid}",))
    else:
        s.db.set_kv(f"biorxiv:{kind}:{pid}", json.dumps(values))


def profiles(s) -> list[str]:
    return [pid for pid, p in s.cfg.profiles.items() if p.get("biorxiv") and p["biorxiv"].get("enabled", True)
            and "biorxiv" in s.access.allowed(pid)]


# ---- digest --------------------------------------------------------------------------------

def build(s, pid: str) -> tuple[str | None, list[biorxiv.Paper], date]:
    """Blocking. Returns (message or None if nothing new, papers in it, end date)."""
    conf = s.cfg.profiles[pid]["biorxiv"]
    today = s.today()
    last = s.db.get_kv(f"biorxiv:last:{pid}")
    start = max(date.fromisoformat(last) if last else today - timedelta(days=2), today - timedelta(days=7))
    papers = biorxiv.fetch_papers(start, today, conf.get("categories") or [], conf.get("include_revisions", False))
    picked = biorxiv.select(papers, terms(s, pid), s.db.biorxiv_sent(pid), terms(s, pid, "context"))
    log.info("bioRxiv %s: %d papers %s..%s, %d match", pid, len(papers), start, today, len(picked))
    if not picked:
        return None, [], today
    cap = int(conf.get("max_papers", 12))
    top, extra = picked[:cap], picked[cap:cap + 20]
    llm_fn = (lambda p: s.llm("brief_models", p, json_output=True, retries=True)) if s.cfg.gemini_api_key else None
    summaries = biorxiv.summarize(top, llm_fn)
    return biorxiv.format_digest(top, summaries, extra, start, today), top + extra, today


# ---- recap after a quiet week --------------------------------------------------------------

def _recap_conf(s, pid: str) -> tuple[int, int]:
    conf = s.cfg.profiles[pid]["biorxiv"]
    return int(conf.get("recap_after_days", 7)), int(conf.get("recap_window_days", 14))


def recap_due(s, pid: str, now: datetime | None = None) -> bool:
    """Nothing sent to this person (digest or recap) for `recap_after_days`. 0 turns recaps off."""
    quiet, _ = _recap_conf(s, pid)
    if quiet <= 0:
        return False
    now = now or datetime.now(UTC)
    last = s.db.get_kv(f"biorxiv:last_msg:{pid}") or s.db.biorxiv_last_sent(pid) or s.db.registered_at(pid)
    return bool(last) and now - datetime.fromisoformat(last) >= timedelta(days=quiet)


def build_recap(s, pid: str) -> str:
    """Blocking (refetches the window from the API). Titles + links only, no AI summaries."""
    quiet, days = _recap_conf(s, pid)
    conf = s.cfg.profiles[pid]["biorxiv"]
    today = s.today()
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    kw, ctx = terms(s, pid), terms(s, pid, "context")
    papers = biorxiv.fetch_papers(today - timedelta(days=days), today, conf.get("categories") or [],
                                  conf.get("include_revisions", False))
    by_doi = {p.doi: p for p in papers}
    sent = []
    for r in s.db.biorxiv_sent_since(pid, since):
        p = by_doi.get(r["doi"])        # rows from before titles were stored: fill in from the refetch
        title, url = r["title"] or (p.title if p else r["doi"]), r["url"] or (p.url if p else "")
        sent.append((title, url))
    already = s.db.biorxiv_sent(pid)
    near = [(p, hits) for p in papers if ctx and p.doi not in already
            and (hits := biorxiv.match_keywords(p, kw)) and not biorxiv.match_keywords(p, ctx)]
    near.sort(key=lambda ph: ph[0].posted, reverse=True)
    log.info("bioRxiv recap %s: %d papers, %d sent, %d near misses", pid, len(papers), len(sent), len(near))
    return biorxiv.format_recap(sent, near[:15], days, quiet, kw, ctx)


async def run(s, force: bool = False, pid: str | None = None) -> None:
    """Scheduled per person (profiles.<id>.biorxiv.schedule); silent when nothing new matched."""
    for p in [pid] if pid else profiles(s):
        if not s.access.receives(p, "biorxiv") and not force:
            continue
        if not s.db.chat_ids_for([p]):
            continue                                     # not registered yet: papers wait for them
        try:
            text, papers, end = await asyncio.to_thread(build, s, p)
            monitoring.note(f"{p}: {len(papers)} paper(s)")
            if text:
                if not await s.n.send_text([p], text):
                    continue                             # nothing delivered: keep them for next time
                s.db.mark_biorxiv_sent(p, [(x.doi, x.title, x.url) for x in papers])   # only after it went out
                s.db.set_kv(f"biorxiv:last_msg:{p}", datetime.now(UTC).isoformat())
            elif force:
                await s.n.send_text([p], "📚 No new matching bioRxiv papers.")
            elif recap_due(s, p):                        # a quiet week: show it still works + near misses
                if await s.n.send_text([p], await asyncio.to_thread(build_recap, s, p)):
                    s.db.set_kv(f"biorxiv:last_msg:{p}", datetime.now(UTC).isoformat())
                    monitoring.note(f"{p}: quiet-week recap")
            s.db.set_kv(f"biorxiv:last:{p}", end.isoformat())
        except Exception as e:  # noqa: BLE001
            await s.report_error(f"biorxiv for {p}", e)


def profile_jobs(s) -> list[tuple[str, object, dict, object]]:
    """Each person's own schedule (cron string or dict)."""
    return [(f"papers:{pid}", run, {"pid": pid},
             s.cfg.profiles[pid]["biorxiv"].get("schedule", {"day_of_week": "mon-fri", "hour": 8}))
            for pid in profiles(s)]


# ---- commands ------------------------------------------------------------------------------

SUBCOMMANDS = ("topics", "keywords", "list", "add", "del", "rm", "remove", "reset", "context")


async def cmd_papers(bot, update, ctx) -> None:
    """/papers · /papers topics · /papers [context] add|del|reset ..."""
    if (ctx.args or [""])[0].lower() in SUBCOMMANDS:
        return await cmd_terms(bot, update, ctx)
    if not (pid := await bot._feature_profile(update, "biorxiv")):
        return
    if not bot.services.cfg.profiles[pid].get("biorxiv"):
        await bot._reply(update, "No bioRxiv topics configured for you (profiles.&lt;id&gt;.biorxiv).")
        return
    await bot._reply(update, "Looking for new bioRxiv papers… (about a minute)")
    await run(bot.services, force=True, pid=pid)


async def cmd_terms(bot, update, ctx) -> None:
    """Edit keywords, or with `context` the terms a paper must also mention."""
    if not (pid := await bot._feature_profile(update, "biorxiv")):
        return
    s = bot.services
    if not s.cfg.profiles[pid].get("biorxiv"):
        await bot._reply(update, "No bioRxiv setup for you (profiles.&lt;id&gt;.biorxiv).")
        return
    words = update.effective_message.text.split()[1:]          # drop "/papers" or "/topics"
    kind = "keywords"
    if words and words[0].lower() == "context":
        kind, words = "context", words[1:]
    action = words[0].lower() if words else "show"
    action = "show" if action in ("topics", "keywords", "list") else action
    arg = " ".join(words[1:]).strip()
    current = terms(s, pid, kind)
    prefix = "/papers context" if kind == "context" else "/papers"
    note = ""
    if action == "add" and arg:
        if arg.lower() in (k.lower() for k in current):
            note = "Already there."
        else:
            current.append(arg)
            set_terms(s, pid, current, kind)
            note = f"Added: {html.escape(arg)}"
    elif action in ("del", "rm", "remove") and arg:
        idx = int(arg) - 1 if arg.isdigit() else next(
            (i for i, k in enumerate(current) if biorxiv.label(k).lower() == arg.lower() or k.lower() == arg.lower()), -1)
        if 0 <= idx < len(current):
            note = f"Removed: {html.escape(current.pop(idx))}"
            set_terms(s, pid, current, kind)
        else:
            note = f"Not found — use the number from {prefix}{'' if kind == 'context' else ' topics'}."
    elif action == "reset":
        set_terms(s, pid, None, kind)
        note = "Back to the configured list."
    elif action != "show":
        note = f"Usage: {prefix} · {prefix} add term|alt · {prefix} del 2 · {prefix} reset"
    kws, context = terms(s, pid), terms(s, pid, "context")
    lines = ([note, ""] if note else [])
    lines += ["<b>Keywords</b> (order = priority; | separates alternative spellings)"]
    lines += [f"{i}. {html.escape(k)}" for i, k in enumerate(kws, 1)] or ["(none)"]
    lines += ["", "<b>Context</b> — a paper must also mention one of these" if context else
              "<b>Context</b>: none (keywords alone decide)"]
    lines += [f"{i}. {html.escape(k)}" for i, k in enumerate(context, 1)]
    cats = s.cfg.profiles[pid]["biorxiv"].get("categories") or []
    lines.append(f"\n<i>Categories: {html.escape(', '.join(cats)) if cats else 'all of bioRxiv'}</i>")
    await bot._reply(update, "\n".join(lines))


FEATURE = Feature(
    name="biorxiv",
    description="bioRxiv papers on your topics, summarized (/papers)",
    commands=(Command("papers", "New bioRxiv papers on your topics", cmd_papers,
                      ("/papers topics — your keywords and context terms",
                       "/papers add zebrafish|danio — add a keyword (| = other spellings)",
                       "/papers del 2 — remove a keyword",
                       "/papers context add cornea — terms a paper must also mention",
                       "/papers reset — back to the configured list")),),
    aliases=(Alias("topics", cmd_terms, "/papers topics"),),
    jobs=(Job("papers", run),),
    profile_jobs=profile_jobs,
    ready=lambda s, pid: bool(s.cfg.profiles.get(pid, {}).get("biorxiv")),
)
