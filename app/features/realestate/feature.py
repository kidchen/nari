"""Redfin alerts (from saved-search emails) delivered per person, plus weekly mortgage rates."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.features.realestate import listings as realestate
from app.features.realestate import mortgage, redfin_email
from app.registry import Feature, Job, MailQuery

log = logging.getLogger(__name__)
MAX_PHOTO_ALERTS = 8  # per person per check; beyond that, captions are sent as plain text


def collect_listings(s, mails) -> tuple[list[realestate.Listing], list[str]]:
    """Blocking: parse Redfin emails. Returns (listings, saved-search labels)."""
    em = s.cfg.settings["real_estate"]["email"]
    llm_fn = (lambda p: s.llm("extraction_models", p)) if em.get("llm_fallback") and s.cfg.gemini_api_key else None
    found_all, searches = [], []
    for m in mails:
        found = redfin_email.parse_email(m.subject, m.html, llm_fn)
        if not found:
            log.info("No listings in Redfin email %r", m.subject)
        found_all += found
        if (meta := redfin_email.parse_search_meta(m.html)) and meta not in searches:
            searches.append(meta)
    return found_all, searches


async def handle_mail(s, mails, force: bool = False) -> None:
    """New Redfin emails -> stored events -> delivered per person. Silent when nothing is new."""
    re_cfg = s.cfg.settings["real_estate"]
    try:
        new_events = 0
        if mails:
            raw, searches = await asyncio.to_thread(collect_listings, s, mails)
            kept = realestate.filter_listings(raw, re_cfg["zips"], re_cfg.get("max_price"))
            diff = realestate.diff_and_store(s.db, kept)
            resolved = {l.id: l for l in await asyncio.to_thread(redfin_email.with_resolved_links, diff.reported())}
            for l in resolved.values():
                s.db.update_listing_url(l.id, l.url)
            diff.new = [resolved[l.id] for l in diff.new]
            diff.price_changes = [(resolved[l.id], old) for l, old in diff.price_changes]
            diff.back_on_market = [resolved[l.id] for l in diff.back_on_market]
            diff.sold = [resolved[l.id] for l in diff.sold]
            diff.similar = [resolved[l.id] for l in diff.similar]
            search = searches[0] if len(searches) == 1 else ""
            meta = lambda l: {"zip": l.zip, "city": l.city, "price": l.price, "search": search}
            for l, cap in realestate.alert_items(diff, search):
                new_events += s.db.add_event(f"{l.id}:{l.kind}:{l.price}",
                                             {"type": "photo", "caption": cap, "photo": l.photo, **meta(l)})
            for l, group, line in realestate.summary_items(diff):
                new_events += s.db.add_event(f"{l.id}:{group}:{l.price}",
                                             {"type": "line", "group": group, "line": line, **meta(l)})
            # Events are stored, so the emails are done even if a delivery below fails.
            s.db.mark_emails_processed([m.message_id for m in mails])
        delivered = await deliver(s)
        if force and not delivered:
            note = f"{len(mails)} Redfin email(s), nothing new" if mails else "No new Redfin emails"
            await s.n.send_text(s.access.recipients("realestate"), f"🏡 {note} since the last check.")
        log.info("Real estate: %d emails, %d new events, %d deliveries", len(mails), new_events, delivered)
    except Exception as e:  # noqa: BLE001
        await s.report_error("realestate", e)


async def deliver(s) -> int:
    """Send each person the events they haven't received: new ones, plus a catch-up window for people who
    registered or resumed recently. A failure stops only that person's queue; it's retried next check."""
    re_cfg = s.cfg.settings["real_estate"]
    window_start = (datetime.now(UTC) - timedelta(days=re_cfg.get("catch_up_days", 3))).isoformat()
    photos: dict[str, bytes | None] = {}
    delivered, failed = 0, []
    for pid in s.access.recipients("realestate"):
        registered = s.db.registered_at(pid)
        if not registered or not s.db.chat_ids_for([pid]):
            continue                                   # not registered yet: their queue waits
        prefs = s.cfg.profiles.get(pid, {}).get("real_estate")
        sent_photos, lines, line_ids, searches = 0, {}, [], []
        for event_id, ev in s.db.undelivered_events(pid, max(window_start, registered)):
            if not realestate.matches_person(prefs, str(ev.get("zip", "")), ev.get("city", ""),
                                             int(ev.get("price") or 0), ev.get("search", "")):
                s.db.mark_delivered(pid, event_id)     # outside this person's own criteria: skip for good
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
                    if url not in photos and re_cfg.get("photos", True):
                        photos[url] = await asyncio.to_thread(redfin_email.fetch_photo, url)
                    if photos.get(url) and sent_photos < MAX_PHOTO_ALERTS:
                        await s.n.send_photo([pid], photos[url], ev["caption"])
                        sent_photos += 1
                    else:
                        await s.n.send_text([pid], ev["caption"])
                else:
                    await s.n.send_text([pid], ev["text"])
                s.db.mark_delivered(pid, event_id)
                delivered += 1
            except Exception as e:  # noqa: BLE001
                log.warning("Delivery to %s failed at %s: %s", pid, event_id, e)
                failed.append(f"{pid}: {e}")
                lines, line_ids = {}, []               # retry everything for this person next check
                break                                  # keep order
        if line_ids:
            try:
                await s.n.send_text([pid], realestate.format_summary(lines, searches))
                for event_id in line_ids:
                    s.db.mark_delivered(pid, event_id)
                delivered += len(line_ids)
            except Exception as e:  # noqa: BLE001
                log.warning("Summary to %s failed: %s", pid, e)
                failed.append(f"{pid}: {e}")
    if failed:
        await s.report_error("realestate delivery", RuntimeError("; ".join(failed)))
    return delivered


async def run_check(s, force: bool = False) -> None:
    """/run realestate: just this feature's Gmail search (the scheduled `mail` job covers all features)."""
    await s.mail_check(force, only=["realestate"])


async def run_mortgage(s, force: bool = False) -> None:
    try:
        rates = await asyncio.to_thread(mortgage.fetch_rates, s.cfg.settings["mortgage"]["series"])
        await s.n.send_text(s.access.recipients("realestate"), mortgage.format_rates(rates))
    except Exception as e:  # noqa: BLE001
        await s.report_error("mortgage", e)


FEATURE = Feature(
    name="realestate",
    description="Redfin alerts with photos + weekly mortgage rates",
    jobs=(Job("realestate", run_check),
          Job("mortgage", run_mortgage,
              lambda st: st["mortgage"]["schedule"] if st.get("real_estate", {}).get("enabled") else None)),
    mail=MailQuery(criteria=lambda st: ["FROM", f'"{st["real_estate"]["email"]["sender"]}"'],
                   lookback_days=lambda st: st["real_estate"]["email"]["lookback_days"],
                   mailboxes=lambda st: (st["real_estate"]["email"].get("mailbox", "INBOX"),),
                   handle=handle_mail),
    extra={"settings_key": "real_estate"},
)
