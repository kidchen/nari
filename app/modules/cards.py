"""Module D: per-person cards and their quarterly 5% categories.

Where a quarter's categories come from, in priority order:
  1. /setcats (manual correction)          kv  rotating:<card_id>:<quarter>
  2. issuer email (automatic, rotating.py)  kv  rotq:<issuer>:<quarter>   (shared: categories are public)
Activation status + the email's personal Activate link are per person: kv rotact:<profile>:<issuer>:<quarter>,
plus the "I activated" button (kv activated:<card_id>:<quarter>).
  3. cards.yaml `rotating.quarters`         static config
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.modules.rotating import QuarterInfo


def quarter_key(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def next_quarter_key(d: date) -> str:
    y, q = d.year, (d.month - 1) // 3 + 1
    return f"{y + 1}Q1" if q == 4 else f"{y}Q{q + 1}"


def cards_for(cards: dict[str, Any], profile: str) -> dict[str, Any]:
    return {cid: c for cid, c in cards.items() if c.get("owner") == profile}


def override_key(card_id: str, qkey: str) -> str:
    return f"rotating:{card_id}:{qkey}"


def activated_key(card_id: str, qkey: str) -> str:
    return f"activated:{card_id}:{qkey}"


def mark_activated(db: Any, card_id: str, qkey: str) -> None:
    db.set_kv(activated_key(card_id, qkey), "1")


def email_key(issuer: str, qkey: str) -> str:
    return f"rotq:{issuer}:{qkey}"


def save_email_quarter(db: Any, info: QuarterInfo) -> None:
    """Shared, public part only: categories/cap/preview are the same for every cardholder."""
    shared = QuarterInfo(info.issuer, info.quarter, info.categories, info.cap, None, info.preview, info.source)
    db.set_kv(email_key(info.issuer, info.quarter), shared.to_json())


def owner_status_key(profile: str, issuer: str, qkey: str) -> str:
    return f"rotact:{profile}:{issuer}:{qkey}"


def save_owner_status(db: Any, profile: str, info: QuarterInfo) -> bool:
    """Personal part: this person's activation status + their own activation link. Returns True if changed.
    Activation is sticky: a later 'activation required' email can't undo a confirmed one."""
    key = owner_status_key(profile, info.issuer, info.quarter)
    old = json.loads(db.get_kv(key) or "{}")
    new = {"activated": True if (old.get("activated") or info.activated) else
           (info.activated if info.activated is not None else old.get("activated")),
           "activate_url": info.activate_url or old.get("activate_url", "")}
    db.set_kv(key, json.dumps(new))
    return new != old


def load_owner_status(db: Any, profile: str, issuer: str, qkey: str) -> dict:
    return json.loads(db.get_kv(owner_status_key(profile, issuer, qkey)) or "{}")


def load_email_quarter(db: Any, issuer: str, qkey: str) -> QuarterInfo | None:
    raw = db.get_kv(email_key(issuer, qkey)) if issuer else None
    return QuarterInfo(**json.loads(raw)) if raw else None


@dataclass
class Rotation:
    categories: list[str]
    source: str                 # manual | email | config | none
    cap: int | None = None
    activated: bool | None = None
    preview: bool = False
    activate_url: str = ""


def rotation(db: Any, card_id: str, card: dict[str, Any], qkey: str) -> Rotation | None:
    """This card's 5% setup for a quarter, or None if the card doesn't rotate."""
    rot = card.get("rotating")
    if not rot:
        return None
    r = _rotation(db, card_id, rot, qkey)
    mine = load_owner_status(db, card.get("owner", ""), rot.get("issuer", ""), qkey)   # this person's own status
    r.activated, r.activate_url = mine.get("activated"), mine.get("activate_url", "")
    if db.get_kv(activated_key(card_id, qkey)):   # confirmed with the bot's "I activated" button
        r.activated = True
    return r


def _rotation(db: Any, card_id: str, rot: dict[str, Any], qkey: str) -> Rotation:
    cap = rot.get("cap_per_quarter")
    if raw := db.get_kv(override_key(card_id, qkey)):
        return Rotation(json.loads(raw), "manual", cap)
    if info := load_email_quarter(db, rot.get("issuer", ""), qkey):
        return Rotation(info.categories, "email", info.cap or cap, None, info.preview)
    if cats := (rot.get("quarters") or {}).get(qkey):
        return Rotation(list(cats), "config", cap)
    return Rotation([], "none", cap)


def _activation_line(card: dict[str, Any], r: Rotation) -> str | None:
    rot = card["rotating"]
    if not rot.get("requires_activation") or r.preview:
        return None
    if r.activated:
        return "  ✅ activated"
    link = f' — <a href="{html.escape(r.activate_url)}">activate</a>' if r.activate_url else " — activate in the app"
    if not rot.get("confirms_activation", True):
        # Issuer never emails a confirmation (e.g. Discover): the person taps the button below once done.
        return "  👉 <b>activate</b>" + link + " · then tap ✅ below"
    return "  👉 <b>activation needed</b>" + link if r.activated is False else "  ❓ activation not confirmed" + link


def quarterly_message(cards: dict[str, Any], profile: str, profile_name: str, today: date, db: Any) -> str | None:
    mine = {cid: c for cid, c in cards_for(cards, profile).items() if c.get("rotating")}
    if not mine:
        return None
    qkey, nkey = quarter_key(today), next_quarter_key(today)
    lines = [f"💳 <b>{qkey} 5% categories</b> — {html.escape(profile_name)}"]
    for cid, c in mine.items():
        r = rotation(db, cid, c, qkey)
        rate, unit = c["rotating"].get("rate", 5), c.get("unit", "%")
        lines.append("")
        lines.append(f"<b>{html.escape(c['name'])}</b>")
        if r and r.categories:
            cap = f" · up to ${r.cap:,}" if r.cap else ""
            lines.append(f"  {rate}{unit}: {html.escape(', '.join(r.categories))}{cap}")
            if act := _activation_line(c, r):
                lines.append(act)
        else:
            lines.append(f"  ⚠️ not known yet — forward the issuer's 5% email, or /setcats {cid} A, B")
        nxt = rotation(db, cid, c, nkey)
        if nxt and nxt.categories:
            lines.append(f"  <i>Next ({nkey}{', preview' if nxt.preview else ''}): "
                         f"{html.escape(', '.join(nxt.categories))}</i>")
    return "\n".join(lines)


def activation_buttons(cards: dict[str, Any], profile: str, today: date, db: Any) -> list[tuple[str, str]]:
    """(label, callback data) for this person's cards that need a manual 'I activated' confirmation."""
    qkey, out = quarter_key(today), []
    for cid, c in cards_for(cards, profile).items():
        rot = c.get("rotating") or {}
        if rot.get("requires_activation") and not rot.get("confirms_activation", True):
            r = rotation(db, cid, c, qkey)
            if r and r.categories and not r.activated:
                out.append((f"✅ I activated {c['name']}", f"act:{cid}:{qkey}"))
    return out


def needs_activation(cards: dict[str, Any], profile: str, today: date, db: Any) -> list[str]:
    """Names of this person's cards whose current quarter isn't confirmed activated."""
    out = []
    for cid, c in cards_for(cards, profile).items():
        if (c.get("rotating") or {}).get("requires_activation"):
            r = rotation(db, cid, c, quarter_key(today))
            if r and r.categories and not r.activated:
                out.append(c["name"])
    return out


def best_card(cards: dict[str, Any], profile: str, category: str, today: date, db: Any) -> list[tuple[float, str, str]]:
    """Rank the profile's cards for a category: list of (rate, card name, reason)."""
    qkey = quarter_key(today)
    cat = category.strip().lower()
    ranked: list[tuple[float, str, str]] = []
    for cid, c in cards_for(cards, profile).items():
        rates = {k.lower(): float(v) for k, v in (c.get("rates") or {}).items()}
        rate, reason = rates.get(cat, rates.get("other", 0.0)), "base" if cat in rates else "everything else"
        r = rotation(db, cid, c, qkey)
        cats = [x.lower() for x in (r.categories if r else [])]
        if any(cat in x or x in cat for x in cats) and float(c["rotating"]["rate"]) > rate:
            rate, reason = float(c["rotating"]["rate"]), f"{qkey} 5% category"
        ranked.append((rate, c["name"], reason))
    return sorted(ranked, reverse=True)
