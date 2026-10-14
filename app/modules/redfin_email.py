"""Parse Redfin alert emails into listings.

Redfin emails render each home as a "homecard": a hero image whose URL embeds the Redfin
listing id, then price (a struck-through second price = previous price), "N Beds N Baths
N Sq Ft", street address and "City, ST 12345". All links are click-tracking redirects,
which still land on the listing, so we keep them as-is.

The deterministic parser handles that layout. If a Redfin email yields no cards (new
template), an optional LLM fallback extracts the same fields from the email text.
"""
from __future__ import annotations

import html as htmlmod
import json
import logging
import re
from typing import Callable

from app.modules.realestate import Listing

log = logging.getLogger(__name__)

HERO_RE = re.compile(r'api-get-listing-hero-shot/(\d+)/')
PRICE_RE = re.compile(r'\$\s?([\d,]+)')
WAS_RE = re.compile(r'line-through[^>]*>\s*\$\s?([\d,]+)')
BEDS_RE = re.compile(r'([\d.]+)\s*Beds?\b', re.I)
BATHS_RE = re.compile(r'([\d.]+)\s*Baths?\b', re.I)
SQFT_RE = re.compile(r'([\d,]+)\s*Sq\.?\s*Ft', re.I)
CITY_RE = re.compile(r"^\s*([A-Za-z][A-Za-z .'\-]+),\s*([A-Z]{2})\s+(\d{5})\s*$")
ANCHOR_RE = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.S | re.I)
HREF_RE = re.compile(r'href="([^"]*)"', re.I)
STREET_RE = re.compile(r'^\s*\d+[A-Za-z]?\s+\S')
SAFELINKS_RE = re.compile(r'https://[a-z0-9.]*safelinks\.protection\.outlook\.com/\?url=([^&"]+)[^"]*')


ACCOUNT_RE = re.compile(r"passcode|password|verif|sign[- ]?in|log[- ]?in|security|confirm your|account", re.I)
LISTING_HINT_RE = re.compile(r"\bhomes?\b|listing|price|sold|open house|for sale|search|match|tour", re.I)


def is_account_email(subject: str) -> bool:
    """Passcodes, verification, password resets: never parse, never send to the LLM."""
    return bool(ACCOUNT_RE.search(subject))


def classify(subject: str) -> str:
    s = subject.lower()
    if "sold" in s:
        return "sold"
    if "price" in s and any(w in s for w in ("drop", "reduc", "cut", "lower", "change")):
        return "price_drop"
    if "open house" in s:
        return "open_house"
    return "for_sale"


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _num(s: str | None) -> float | None:
    try:
        return float(s.replace(",", "")) if s else None
    except ValueError:
        return None


def unwrap_safelinks(html: str) -> str:
    from urllib.parse import unquote

    return SAFELINKS_RE.sub(lambda m: unquote(m.group(1)), html)


def parse_cards(html: str, kind: str = "for_sale") -> list[Listing]:
    html = unwrap_safelinks(html)
    heroes = list(HERO_RE.finditer(html))
    out: list[Listing] = []
    for i, hero in enumerate(heroes):
        seg = html[hero.start(): heroes[i + 1].start() if i + 1 < len(heroes) else len(html)]
        text = _text(seg)
        prices = PRICE_RE.findall(text)
        if not prices:
            continue
        was = WAS_RE.search(seg)
        address = city = zip_ = state = ""
        url = ""
        anchors = [((m.group(1) if (m := HREF_RE.search(attrs)) else ""), _text(t))
                   for attrs, t in ANCHOR_RE.findall(seg)]
        for j, (href, t) in enumerate(anchors):
            if not address and STREET_RE.match(t) and "$" not in t and not BEDS_RE.search(t):
                address, url = t, htmlmod.unescape(href)
                for _, t2 in anchors[j + 1: j + 3]:
                    if m := CITY_RE.match(t2):
                        city, state, zip_ = m.groups()
                        break
        if not address or not zip_:
            continue
        beds, baths, sqft = BEDS_RE.search(text), BATHS_RE.search(text), SQFT_RE.search(text)
        out.append(Listing(
            url=url or f"https://www.redfin.com/?listing={hero.group(1)}",
            address=address, city=city, zip=zip_,
            price=int(_num(prices[0]) or 0),
            beds=int(_num(beds.group(1))) if beds else None,
            sqft=int(_num(sqft.group(1))) if sqft else None,
            home_type="",
            baths=_num(baths.group(1)) if baths else None,
            key=f"redfin:{hero.group(1)}",
            kind=kind,
            was_price=int(_num(was.group(1))) if was else None,
        ))
    return out


LLM_PROMPT = """Extract every home listing from this Redfin email text. Return ONLY a JSON array.
Each item: {"address": str, "city": str, "zip": "5-digit string", "price": int, "was_price": int|null,
"beds": int|null, "baths": number|null, "sqft": int|null}. Use only values present in the text.
If there are no listings, return [].

EMAIL TEXT:
"""


def parse_with_llm(html: str, kind: str, summarize: Callable[[str], str | None]) -> list[Listing]:
    text = _text(re.sub(r"<(style|head)\b.*?</\1>", " ", html, flags=re.S | re.I))[:15000]
    raw = summarize(LLM_PROMPT + text)
    if not raw:
        return []
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON for Redfin email")
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        try:
            zip_ = str(it["zip"]).strip()[:5]
            price = int(it["price"])
            address = str(it["address"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not (re.fullmatch(r"\d{5}", zip_) and address and price > 0):
            continue
        out.append(Listing(
            url=f"https://www.redfin.com/zipcode/{zip_}", address=address,
            city=str(it.get("city") or ""), zip=zip_, price=price,
            beds=_int(it.get("beds")), sqft=_int(it.get("sqft")), home_type="",
            baths=_num(str(it["baths"])) if it.get("baths") is not None else None,
            key=f"addr:{address.lower()}|{zip_}", kind=kind, was_price=_int(it.get("was_price")),
        ))
    return out


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_email(subject: str, html: str, llm: Callable[[str], str | None] | None = None) -> list[Listing]:
    if is_account_email(subject):
        return []
    kind = classify(subject)
    cards = parse_cards(html, kind)
    if not cards and llm and "$" in html and LISTING_HINT_RE.search(subject):
        log.info("No homecards found in %r; trying LLM extraction", subject)
        cards = parse_with_llm(html, kind, llm)
    return cards
