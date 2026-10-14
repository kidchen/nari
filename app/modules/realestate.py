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
    kind: str = "for_sale"         # for_sale | price_drop | sold | open_house
    was_price: int | None = None   # struck-through previous price shown in the email

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


def filter_listings(listings: Iterable[Listing], zips: dict[str, str], max_price: int) -> list[Listing]:
    """Keep single-family homes in target zips at or under max price. Later entries win on duplicates."""
    kept: dict[str, Listing] = {}
    for l in listings:
        if l.zip in zips and 0 < l.price <= max_price and is_single_family(l):
            kept[l.id] = l
    return sorted(kept.values(), key=lambda l: (zips[l.zip], l.price))


@dataclass
class Diff:
    new: list[Listing] = field(default_factory=list)
    price_changes: list[tuple[Listing, int]] = field(default_factory=list)  # (listing, old price)
    sold: list[Listing] = field(default_factory=list)


def diff_and_store(db: DB, listings: list[Listing]) -> Diff:
    d = Diff()
    for l in listings:
        prev = db.get_listing(l.id)
        if l.kind == "sold":
            if not prev or prev["kind"] != "sold":
                d.sold.append(l)
        elif prev is None or prev["kind"] == "sold":
            if l.kind == "price_drop" and l.was_price:
                d.price_changes.append((l, l.was_price))
            else:
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
    return (f'• <a href="{html.escape(l.url)}">{html.escape(l.address)}</a>, {html.escape(l.city)} '
            f"— <b>${l.price:,}</b>" + (f" · {tail}" if tail else ""))


def format_report(d: Diff, max_price: int, emails_read: int, errors: list[str]) -> str:
    lines = [f"🏡 <b>Single-family homes ≤ ${max_price:,}</b> — from {emails_read} Redfin email(s)"]
    if d.new:
        lines += ["", f"<b>🆕 New listings ({len(d.new)})</b>"] + [_fmt(l) for l in d.new]
    if d.price_changes:
        lines += ["", f"<b>💲 Price changes ({len(d.price_changes)})</b>"]
        for l, old in d.price_changes:
            arrow = "🔻" if l.price < old else "🔺"
            lines.append(f"{_fmt(l)} {arrow} was ${old:,}")
    if d.sold:
        lines += ["", f"<b>🏷️ Recently sold nearby ({len(d.sold)})</b>"]
        lines += [_fmt(l) + (f" (listed ${l.was_price:,})" if l.was_price else "") for l in d.sold]
    if not (d.new or d.price_changes or d.sold):
        lines += ["", "No new matching listings since last report."]
    if errors:
        lines += ["", "⚠️ " + html.escape("; ".join(errors[:3]))]
    return "\n".join(lines)
