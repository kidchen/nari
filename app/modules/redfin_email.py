"""Parse Redfin alert emails into listings.

Redfin emails render each home as a "homecard": a hero image (URL embeds the Redfin listing id,
optionally after a hash; its alt text names the event, e.g. "Price Increase"), then price (an older
price is struck through: CSS line-through or <del>), "N Beds · N Baths · N Sq. Ft.", and the address —
either "Street" + "City, ST 12345" in two links (nearby-sales emails) or one combined line
(saved-search alerts). Saved-search alerts also carry the search name + filters, and a
"Nearby similar homes" section that is NOT part of the search.

All links are click-tracking redirects; resolve_link() turns one into the real listing URL.
If an email yields no cards (unknown template), an optional LLM fallback extracts the same fields.
"""
from __future__ import annotations

import html as htmlmod
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from typing import Callable

from app.modules.realestate import Listing

log = logging.getLogger(__name__)

HERO_IMG_RE = re.compile(r'<img\b[^>]*api-get-listing-hero-shot/(?:[0-9a-f]{16,}/)?(\d+)/[^>]*>', re.I)
ALT_RE = re.compile(r'\balt="([^"]*)"', re.I)
SRC_RE = re.compile(r'\bsrc="([^"]+)"', re.I)
PRICE_RE = re.compile(r'\$\s?([\d,]{4,})')
WAS_RE = re.compile(r'(?:line-through[^>]*>|<del\b[^>]*>)\s*\$\s?([\d,]+)', re.I)
BEDS_RE = re.compile(r'([\d.]+)\s*Beds?\b', re.I)
BATHS_RE = re.compile(r'([\d.]+)\s*Baths?\b', re.I)
SQFT_RE = re.compile(r'([\d,]+)\s*Sq\.?\s*Ft\.?(?!\.?\s*lot)', re.I)   # not "5,044 sq ft lot"
CITY_RE = re.compile(r"^\s*([A-Za-z][A-Za-z .'\-]+),\s*([A-Z]{2})\s+(\d{5})\s*$")
FULL_ADDR_RE = re.compile(r"^\s*(\d+[A-Za-z]?\s+[^,]+),\s*([A-Za-z][A-Za-z .'\-]+),\s*([A-Z]{2})\s+(\d{5})\s*$")
ANCHOR_RE = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.S | re.I)
HREF_RE = re.compile(r'href="([^"]*)"', re.I)
STREET_RE = re.compile(r'^\s*\d+[A-Za-z]?\s+\S')
SAFELINKS_RE = re.compile(r'https://[a-z0-9.]*safelinks\.protection\.outlook\.com/\?url=([^&"]+)[^"]*')
SIMILAR_RE = re.compile(r"Nearby similar homes|Related to your search|Similar homes", re.I)
GENERIC_ALTS = {"", "alt text here", "image of your home", "home photo"}

ACCOUNT_RE = re.compile(r"passcode|password|verif|sign[- ]?in|log[- ]?in|security|confirm your|account", re.I)
LISTING_HINT_RE = re.compile(r"\bhomes?\b|listing|price|sold|open house|for sale|search|match|tour", re.I)


def is_account_email(subject: str) -> bool:
    """Passcodes, verification, password resets: never parse, never send to the LLM."""
    return bool(ACCOUNT_RE.search(subject))


def classify(text: str) -> str:
    """Event kind from a subject line or a homecard's alt text."""
    s = text.lower()
    if "sold" in s:
        return "sold"
    if "price" in s and any(w in s for w in ("drop", "reduc", "cut", "lower", "change", "increase", "raise")):
        return "price_change"
    if "back on market" in s:
        return "back_on_market"
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


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def unwrap_safelinks(html: str) -> str:
    return SAFELINKS_RE.sub(lambda m: urllib.parse.unquote(m.group(1)), html)


def _anchors(seg: str) -> list[tuple[str, str]]:
    return [((htmlmod.unescape(m.group(1)) if (m := HREF_RE.search(attrs)) else ""), _text(t))
            for attrs, t in ANCHOR_RE.findall(seg)]


def _address(anchors: list[tuple[str, str]]) -> tuple[str, str, str, str]:
    """(street, city, zip, link) from a homecard's links; supports one- and two-line addresses."""
    for j, (href, t) in enumerate(anchors):
        if "$" in t or BEDS_RE.search(t):
            continue
        if m := FULL_ADDR_RE.match(t):
            return m.group(1).strip(), m.group(2).strip(), m.group(4), href
        if STREET_RE.match(t):
            for _, t2 in anchors[j + 1: j + 3]:
                if m := CITY_RE.match(t2):
                    return t.strip(), m.group(1).strip(), m.group(3), href
    return "", "", "", ""


def parse_cards(html: str, default_kind: str = "for_sale") -> list[Listing]:
    html = unwrap_safelinks(html)
    heroes = list(HERO_IMG_RE.finditer(html))
    similar_at = m.start() if (m := SIMILAR_RE.search(html)) else len(html)
    out: list[Listing] = []
    for i, hero in enumerate(heroes):
        seg = html[hero.start(): heroes[i + 1].start() if i + 1 < len(heroes) else len(html)]
        prices = PRICE_RE.findall(_text(seg))
        street, city, zip_, link = _address(_anchors(seg))
        if not prices or not street or not zip_:
            continue
        text = _text(seg)
        alt = (ALT_RE.search(hero.group(0)) or [None, ""])[1].strip()
        event = "" if alt.lower() in GENERIC_ALTS else alt
        was = WAS_RE.search(seg)
        beds, baths, sqft = BEDS_RE.search(text), BATHS_RE.search(text), SQFT_RE.search(text)
        out.append(Listing(
            url=link, address=street, city=city, zip=zip_,
            price=int(_num(prices[0]) or 0),
            beds=int(_num(beds.group(1))) if beds else None,
            sqft=int(_num(sqft.group(1))) if sqft else None,
            baths=_num(baths.group(1)) if baths else None,
            key=f"redfin:{hero.group(1)}",
            kind=classify(event) if event else default_kind,
            was_price=int(_num(was.group(1))) if was else None,
            event=event,
            section="similar" if hero.start() > similar_at else "alert",
            photo=htmlmod.unescape(m.group(1)) if (m := SRC_RE.search(hero.group(0))) else "",
        ))
    return out


def parse_search_meta(html: str) -> str:
    """Saved search name + filters, e.g. 'my search · <$1.5M, House' ('' if not a saved-search alert)."""
    body = re.sub(r"<(style|head)\b.*?</\1>", " ", html, flags=re.S | re.I)
    lines = [l for l in (_text(x) for x in re.split(r"<br\s*/?>|</(?:p|div|tr|td|h\d|span|a)>", body)) if l]
    for i, line in enumerate(lines):
        if re.fullmatch(r"(New )?Saved Search", line, re.I) and i + 1 < len(lines):
            name = lines[i + 1]
            crit = lines[i + 2] if i + 2 < len(lines) and re.search(r"\$|House|Condo|Town|Bed|Bath", lines[i + 2]) else ""
            return f"{name} · {crit}" if crit else name
    return ""


LLM_PROMPT = """Extract every home listing from this Redfin email text. Return ONLY a JSON array.
Each item: {"address": "street only", "city": str, "zip": "5-digit string", "price": int, "was_price": int|null,
"beds": int|null, "baths": number|null, "sqft": int|null, "similar": true if listed under a 'similar/nearby
homes' heading rather than as the alert itself}. Use only values present in the text. If none, return [].

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
    anchors = _anchors(unwrap_safelinks(html))
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
        # The model only sees text; recover the real link from the anchor that shows this address.
        link = next((h for h, t in anchors if h and t.lower().startswith(address.lower())), "")
        out.append(Listing(
            url=link, address=address, city=str(it.get("city") or ""), zip=zip_, price=price,
            beds=_int(it.get("beds")), sqft=_int(it.get("sqft")),
            baths=_num(str(it["baths"])) if it.get("baths") is not None else None,
            key=f"addr:{address.lower()}|{zip_}", kind=kind, was_price=_int(it.get("was_price")),
            section="similar" if it.get("similar") else "alert",
        ))
    return out


def parse_email(subject: str, html: str, llm: Callable[[str], str | None] | None = None) -> list[Listing]:
    if is_account_email(subject):
        return []
    kind = classify(subject)
    cards = parse_cards(html, kind)
    if not cards and llm and "$" in html and LISTING_HINT_RE.search(subject):
        log.info("No homecards found in %r; trying LLM extraction", subject)
        cards = parse_with_llm(html, kind, llm)
    return cards


# ---- link resolution ----------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102 — stop at the first redirect
        return None


def resolve_link(url: str, timeout: float = 10) -> str:
    """Follow ONE redirect hop of a Redfin tracking link (redmail*.redfin.com) and return the clean
    listing URL (query string dropped). We never load the Redfin page itself. Returns url on failure."""
    host = urllib.parse.urlparse(url).netloc
    if not host.startswith("redmail") or not host.endswith("redfin.com"):
        return url
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 nari"}), timeout=timeout)
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") or ""
        p = urllib.parse.urlparse(loc)
        if p.netloc.endswith("redfin.com") and "/home/" in p.path:
            return urllib.parse.urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    except Exception as e:  # noqa: BLE001
        log.info("Could not resolve Redfin link: %s", e)
    return url


def fetch_photo(url: str, timeout: float = 15, max_bytes: int = 5_000_000) -> bytes | None:
    """Download a listing photo from the email card (redfin.com hero-shot API). None on any failure."""
    if not url or not urllib.parse.urlparse(url).netloc.endswith("redfin.com"):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) nari"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if not r.headers.get("Content-Type", "").startswith("image/"):
                return None
            data = r.read(max_bytes + 1)
            return data if 1000 < len(data) <= max_bytes else None
    except Exception as e:  # noqa: BLE001
        log.info("Photo download failed: %s", e)
        return None


def with_resolved_links(listings: list[Listing], resolve: Callable[[str], str] = resolve_link) -> list[Listing]:
    return [replace(l, url=resolve(l.url)) if l.url else l for l in listings]
