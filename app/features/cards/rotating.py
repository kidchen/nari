"""Quarterly 5% categories from issuer emails (Discover, Chase Freedom), usually forwarded from
another mailbox to the bot's Gmail.

Issuers send an "activate your 5%" email before each quarter and an "activated" confirmation after.
Both state the categories in a stable sentence, so we parse them exactly; an LLM fallback (with
names, email addresses and account digits redacted) covers layout changes.
"""
from __future__ import annotations

import html as htmlmod
import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date

log = logging.getLogger(__name__)

ISSUER_DOMAINS = {"discover": ("discover.com",), "chase": ("chase.com",)}
ORIG_FROM_RE = re.compile(r"From:\s*[^<\n]{0,60}<[^@>\s]+@([\w.-]+)>", re.I)
DATE = r"(\d{1,2}/\d{1,2}/\d{2,4})"

DISCOVER_RE = re.compile(r"Activate to earn 5% Cashback Bonus at (.+?) from " + DATE + r".{0,80}?through " + DATE
                         + r",?\s*on up to \$([\d,]+)", re.I | re.S)
DISCOVER_HEAD_RE = re.compile(r"Get 5% cash back at (.+?)\s+(?:Oct|Jan|Apr|Jul)\w*\.?\s*1\s*[-–]", re.I | re.S)
CHASE_RE = re.compile(r"Earn 5% cash back on up to \$([\d,]+) in combined purchases on (.+?) from " + DATE
                      + r" to " + DATE, re.I | re.S)
CHASE_NEXT_RE = re.compile(r"Get ready for next quarter(.+?)Earn in these categories from " + DATE + r" to " + DATE,
                           re.I | re.S)


@dataclass
class QuarterInfo:
    issuer: str
    quarter: str                    # "2026Q4"
    categories: list[str]
    cap: int | None = None
    activated: bool | None = None   # True: confirmed; False: activation required; None: unknown
    preview: bool = False           # announced early ("get ready for next quarter"), may be refined
    source: str = "email"
    activate_url: str = ""          # the email's own "Activate" button link, if any

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class ParseResult:
    issuer: str | None
    quarters: list[QuarterInfo] = field(default_factory=list)


# ---- helpers ----------------------------------------------------------------------------

BLOCK = " ¶ "  # marks table-cell / paragraph boundaries in email_text(blocks=True)


def email_text(raw_html_or_text: str, blocks: bool = False) -> str:
    t = re.sub(r"<(style|head|script)\b.*?</\1>", " ", raw_html_or_text, flags=re.S | re.I)
    if blocks:
        t = re.sub(r"<br\s*/?>|</(td|p|div|tr|li|h\d)>", BLOCK, t, flags=re.I)
    t = htmlmod.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"[\s\xa0‌͏­‍]+", " ", t).strip()
    return re.sub(r"(\s*¶\s*)+", BLOCK, t) if blocks else t


def quarter_of(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def parse_date(s: str) -> date:
    m, d, y = (int(x) for x in s.split("/"))
    return date(2000 + y if y < 100 else y, m, d)


def split_categories(s: str) -> list[str]:
    """'Dining, Grocery Stores (excluding Walmart ® and Target ® *) and donations to X' -> 3 items."""
    s = re.sub(r"[®™*†]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .,")
    parts, depth, cur = [], 0, ""
    tokens = re.split(r"(\(|\)|,| and )", s)
    for tok in tokens:
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        if depth == 0 and tok in (",", " and "):
            if cur.strip():
                parts.append(cur.strip())
            cur = ""
        else:
            cur += tok
    if cur.strip():
        parts.append(cur.strip())
    out = []
    for p in parts:
        p = re.sub(r"\s+\)", ")", re.sub(r"\(\s+", "(", p)).strip(" .")
        p = re.sub(r"^(and|the)\s+", "", p, flags=re.I)
        if p:
            out.append(p[0].upper() + p[1:])
    return out


def identify_issuer(sender: str, text: str) -> str | None:
    """Direct sender, or the original 'From:' line inside a forwarded email."""
    domains = [sender.lower()] + [m.group(1).lower() for m in ORIG_FROM_RE.finditer(text[:5000])]
    for issuer, doms in ISSUER_DOMAINS.items():
        if any(d.rstrip(">").endswith(dom) for d in domains for dom in doms):
            return issuer
    return None


def activation_from_subject(subject: str) -> bool | None:
    s = subject.lower()
    if re.search(r"activation required|activate (now|your|to)|don.t forget to activate|reminder: activate", s):
        return False
    if re.search(r"(you.re|is|are|been) activated|activation (complete|confirmed)", s):
        return True
    return None


# ---- exact parsers ------------------------------------------------------------------------

def parse_discover(text: str, activated: bool | None) -> list[QuarterInfo]:
    if m := DISCOVER_RE.search(text):
        cats, start, _end, cap = m.groups()
        return [QuarterInfo("discover", quarter_of(parse_date(start)), split_categories(cats),
                            int(cap.replace(",", "")), activated)]
    return []


def parse_chase(text: str, activated: bool | None, blocks: str = "") -> list[QuarterInfo]:
    out = []
    if m := CHASE_RE.search(text):
        cap, cats, start, _end = m.groups()
        out.append(QuarterInfo("chase", quarter_of(parse_date(start)), split_categories(cats),
                               int(cap.replace(",", "")), activated))
    # The preview lists one category per table cell; use the block-separated text to keep them apart.
    if m := CHASE_NEXT_RE.search(blocks or text):
        cats, start, _end = m.groups()
        items = [c for chunk in cats.split("¶") for c in split_categories(chunk) if c and len(c) < 60
                 and not re.fullmatch(r"get ready for next quarter", c, re.I)]
        if items:
            out.append(QuarterInfo("chase", quarter_of(parse_date(start)), items, preview=True))
    return out


# ---- LLM fallback --------------------------------------------------------------------------

def redact(text: str) -> str:
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
    text = re.sub(r"(ending (in|with)|account number|intended for|sent to)[:\s]+[^.\n]{0,40}", r"\1 [redacted]", text, flags=re.I)
    return re.sub(r"\b\d{4,}\b", "[n]", text)


LLM_PROMPT = """This is a credit card issuer email about quarterly 5% bonus categories. Return ONLY JSON:
{"quarters": [{"start": "M/D/YYYY", "categories": [str], "cap_dollars": int|null, "preview": bool}]}
"preview" = true only for an early announcement of a LATER quarter. Use only what the text says.
If the email has no 5% categories, return {"quarters": []}.

EMAIL:
"""


def parse_with_llm(issuer: str, text: str, activated: bool | None, summarize: Callable[[str], str | None]) -> list[QuarterInfo]:
    raw = summarize(LLM_PROMPT + redact(text)[:8000])
    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M))
    except json.JSONDecodeError:
        return []
    out = []
    for q in data.get("quarters", []) if isinstance(data, dict) else []:
        try:
            cats = [str(c).strip() for c in q["categories"] if str(c).strip()]
            start = parse_date(str(q["start"]))
        except (KeyError, TypeError, ValueError):
            continue
        if cats:
            out.append(QuarterInfo(issuer, quarter_of(start), cats, q.get("cap_dollars"),
                                   None if q.get("preview") else activated, bool(q.get("preview")), "email+llm"))
    return out


ACTIVATE_LINK_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(?:(?!</a>).){0,300}?\bActivate\b', re.I | re.S)


def activate_link(html: str) -> str:
    m = ACTIVATE_LINK_RE.search(html)
    url = htmlmod.unescape(m.group(1)) if m else ""
    return url if url.startswith("https://") else ""


def parse_email(sender: str, subject: str, html_or_text: str,
                llm: Callable[[str], str | None] | None = None) -> ParseResult:
    text = email_text(html_or_text)
    # The forwarded "From: Bank <x@bank.com>" line may be escaped text or raw markup; check both.
    issuer = identify_issuer(sender, text + " " + htmlmod.unescape(html_or_text[:8000]))
    if not issuer or "5%" not in subject + text[:3000]:
        return ParseResult(issuer)
    activated = activation_from_subject(subject)
    quarters = (parse_discover(text, activated) if issuer == "discover"
                else parse_chase(text, activated, email_text(html_or_text, blocks=True)))
    if not quarters and llm:
        log.info("No exact match in %s email %r; trying LLM", issuer, subject)
        quarters = parse_with_llm(issuer, text, activated, llm)
    if activated is False and (link := activate_link(html_or_text)):
        for q in quarters:
            if not q.preview:
                q.activate_url = link
    return ParseResult(issuer, quarters)


def merge(existing: QuarterInfo | None, new: QuarterInfo) -> QuarterInfo:
    """A confirmed (non-preview) announcement beats a preview; activation True is sticky."""
    if existing is None:
        return new
    if existing.preview and not new.preview:
        base = new
    elif new.preview and not existing.preview:
        base = existing
    else:
        base = new
    base.activated = True if (existing.activated or new.activated) else (new.activated if new.activated is not None
                                                                       else existing.activated)
    base.activate_url = new.activate_url or existing.activate_url
    return base
