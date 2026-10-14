"""Module B (part 1): Redfin listings, sourced from Redfin saved-search alert emails in Gmail.

Redfin has no API and bot-checks scrapers, so we let Redfin email us (saved search with
alerts) and read those emails. The saved search should already be filtered to single-family
homes <= max price in the target cities; we re-check zip, price and unit-style addresses here.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from app.db import DB

log = logging.getLogger(__name__)

UNIT_RE = re.compile(r"\b(apt|apartment|unit|suite|ste)\b|#", re.I)


@dataclass(frozen=True)
class Listing:
    url: str
    address: str
    city: str
    zip: str
    price: int
    beds: int | None
    sqft: int | None
    home_type: str = ""
    baths: float | None = None
    key: str = ""
    kind: str = "for_sale"         # for_sale | price_change | sold | open_house | back_on_market
    was_price: int | None = None   # struck-through previous price shown in the email
    event: str = ""                # Redfin's label for the card, e.g. "Price Increase", "Open House"
    section: str = "alert"         # alert = matches your saved search; similar = Redfin's "nearby similar homes"
    photo: str = ""                # Redfin hero-shot image URL from the email card

    @property
    def id(self) -> str:
        return self.key or self.url

    @property
    def price_per_sqft(self) -> int | None:
        return round(self.price / self.sqft) if self.sqft else None


def is_single_family(l: Listing) -> bool:
    if l.home_type:
        return l.home_type == "SingleFamilyResidence"
    return not UNIT_RE.search(l.address)


def filter_listings(listings: Iterable[Listing], zips: dict[str, str], max_price: int | None = None) -> list[Listing]:
    """Keep single-family homes in target zips (and under max_price if set). Later entries win on
    duplicates, except a saved-search alert is never downgraded to a 'similar' suggestion."""
    kept: dict[str, Listing] = {}
    for l in listings:
        if l.zip not in zips or l.price <= 0 or not is_single_family(l):
            continue
        if max_price and l.price > max_price:
            continue
        if l.id in kept and kept[l.id].section == "alert" and l.section == "similar":
            continue
        kept[l.id] = l
    return sorted(kept.values(), key=lambda l: (zips[l.zip], l.price))


@dataclass
class Diff:
    new: list[Listing] = field(default_factory=list)
    price_changes: list[tuple[Listing, int]] = field(default_factory=list)  # (listing, old price)
    back_on_market: list[Listing] = field(default_factory=list)
    sold: list[Listing] = field(default_factory=list)
    similar: list[Listing] = field(default_factory=list)                     # first-time suggestions only

    def reported(self) -> list[Listing]:
        return self.new + [l for l, _ in self.price_changes] + self.back_on_market + self.sold + self.similar


def diff_and_store(db: DB, listings: list[Listing]) -> Diff:
    d = Diff()
    for l in listings:
        prev = db.get_listing(l.id)
        prev_kind = prev["kind"] if prev else None
        if l.section == "similar":
            if prev is None:
                d.similar.append(l)
            if prev is None or prev_kind == "similar":   # never overwrite what an alert told us
                db.upsert_listing(l.id, l.url, l.address, l.price, "similar")
            continue
        if l.kind == "sold":
            if prev_kind != "sold":
                d.sold.append(l)
        elif l.kind == "back_on_market" and prev_kind != "back_on_market":
            d.back_on_market.append(l)
        elif l.kind == "price_change" and l.was_price and (prev is None or prev["price"] != l.price):
            d.price_changes.append((l, l.was_price))
        elif prev is None or prev_kind in ("sold", "similar"):
            d.new.append(l)
        elif prev["price"] != l.price:
            d.price_changes.append((l, prev["price"]))
        db.upsert_listing(l.id, l.url, l.address, l.price, l.kind)
    return d


def _fmt(l: Listing) -> str:
    specs = []
    if l.beds is not None:
        specs.append(f"{l.beds}bd")
    if l.baths is not None:
        specs.append(f"{l.baths:g}ba")
    if l.sqft:
        specs.append(f"{l.sqft:,} sqft")
    if l.price_per_sqft:
        specs.append(f"${l.price_per_sqft:,}/sqft")
    tail = " · ".join(specs)
    addr = html.escape(l.address)
    name = f'<a href="{html.escape(l.url)}">{addr}</a>' if l.url else addr
    return f"• {name}, {html.escape(l.city)} — <b>${l.price:,}</b>" + (f" · {tail}" if tail else "")


# ---- photo alerts ---------------------------------------------------------------------

def _specs(l: Listing) -> str:
    specs = []
    if l.beds is not None:
        specs.append(f"{l.beds} bd")
    if l.baths is not None:
        specs.append(f"{l.baths:g} ba")
    if l.sqft:
        specs.append(f"{l.sqft:,} sqft")
    if l.price_per_sqft:
        specs.append(f"${l.price_per_sqft:,}/sqft")
    return " · ".join(specs)


def caption(l: Listing, label: str, old_price: int | None = None, search: str = "") -> str:
    """Telegram photo caption (HTML, well under the 1024-char limit)."""
    addr = html.escape(l.address)
    name = f'<a href="{html.escape(l.url)}">{addr}</a>' if l.url else addr
    price = f"<b>${l.price:,}</b>"
    if old_price:
        delta = l.price - old_price
        price += f" {'🔺' if delta > 0 else '🔻'} {'+' if delta > 0 else '−'}${abs(delta):,} (was ${old_price:,})"
    lines = [f"<b>{html.escape(label)}</b>", f"{name}, {html.escape(l.city)}", price]
    if specs := _specs(l):
        lines.append(specs)
    if search:
        lines.append(f"<i>{html.escape(search)}</i>")
    return "\n".join(lines)[:1000]


def alert_items(d: Diff, search: str = "") -> list[tuple[Listing, str]]:
    """The listings that deserve their own photo message, with captions."""
    items = [(l, caption(l, "🆕 New listing", search=search)) for l in d.new]
    for l, old in d.price_changes:
        items.append((l, caption(l, "💲 Price " + ("drop" if l.price < old else "increase"), old, search)))
    items += [(l, caption(l, "🔄 Back on market", search=search)) for l in d.back_on_market]
    return items


def summary_text(d: Diff, searches: list[str]) -> str | None:
    """One text message for sold homes and Redfin's suggestions (no photos), or None if neither."""
    if not (d.sold or d.similar):
        return None
    lines = ["🏡 <b>Redfin</b>" + (" — " + "; ".join(f"“{html.escape(s)}”" for s in searches) if searches else "")]
    if d.sold:
        lines += ["", f"<b>🏷️ Sold ({len(d.sold)})</b>"]
        lines += [_fmt(l) + (f" (listed ${l.was_price:,})" if l.was_price else "") for l in d.sold]
    if d.similar:
        lines += ["", f"<b>🏘️ Similar homes Redfin suggests ({len(d.similar)})</b> <i>— may be outside your search</i>"]
        lines += [_fmt(l) for l in d.similar[:6]]
    return "\n".join(lines)
