"""Module C: morning market brief — per-person portfolio + watchlist, with "what happened / why it
matters / what to watch" for each stock that has real news, plus the day's biggest market stories.

Data: Yahoo (prices, volume, earnings dates) + Google News / Yahoo RSS headlines. Gemini only
interprets the facts and headlines we hand it, cites them by id, and never gives buy/sell advice.
"""
from __future__ import annotations

import calendar
import html
import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
GNEWS_SEARCH = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
YAHOO_TICKER_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
# Listicles / evergreen promo headlines that never explain a move.
FLUFF_RE = re.compile(
    r"\$[\d,]+ invest|could be worth|should you (buy|sell|hold)|millionaire|better buy|"
    r"top \d+ stocks|stocks? to (buy|watch|own)|forever stock|no-brainer|is it (too late|a buy)|"
    r"(buy|sell) (it )?(now|today)|closes the market|price prediction|forecast:", re.I)
NAME_SUFFIX_RE = re.compile(r",?\s+(inc\.?|corp(oration)?\.?|co\.?|ltd\.?|plc|holdings?|"
                            r"technologies|class [a-c]|n\.?v\.?|s\.?a\.?)\b.*$", re.I)


@dataclass
class Headline:
    id: str
    title: str
    source: str
    link: str
    age_h: float


@dataclass
class TickerFacts:
    symbol: str
    name: str = ""
    quote_type: str = ""
    price: float | None = None
    change_pct: float | None = None
    change_5d_pct: float | None = None
    volume_pace: float | None = None      # today's volume vs normal, adjusted for time of day
    pct_from_52w_high: float | None = None
    earnings: date | None = None
    headlines: list[Headline] = field(default_factory=list)

    @property
    def is_fund(self) -> bool:
        return self.quote_type in ("ETF", "MUTUALFUND", "INDEX")


# ---- data gathering -------------------------------------------------------------

def session_fraction(now_ny: datetime) -> float:
    """Fraction of the regular session (9:30–16:00 ET) elapsed, clamped to [0.05, 1]."""
    minutes = (now_ny.hour * 60 + now_ny.minute) - (9 * 60 + 30)
    return min(1.0, max(0.05, minutes / 390))


def short_name(name: str) -> str:
    return NAME_SUFFIX_RE.sub("", name).strip() or name


def fetch_facts(symbols: list[str], now_ny: datetime, name_cache: dict[str, tuple[str, str]]) -> dict[str, TickerFacts]:
    """Prices/volume/earnings per symbol. name_cache: symbol -> (name, quote_type), filled in place."""
    import yfinance as yf

    out: dict[str, TickerFacts] = {}
    closes = yf.download(symbols, period="1mo", progress=False, auto_adjust=True)["Close"] if symbols else None
    frac = session_fraction(now_ny)
    for s in symbols:
        f = TickerFacts(s)
        t = yf.Ticker(s)
        try:
            if s not in name_cache:
                info = t.info
                name_cache[s] = (info.get("shortName") or info.get("longName") or s, info.get("quoteType") or "")
            f.name, f.quote_type = name_cache[s]
            fi = t.fast_info
            f.price, prev = fi.last_price, fi.previous_close
            if f.price and prev:
                f.change_pct = (f.price / prev - 1) * 100
            if fi.last_volume and fi.three_month_average_volume:
                f.volume_pace = fi.last_volume / (fi.three_month_average_volume * frac)
            if f.price and fi.year_high:
                f.pct_from_52w_high = (f.price / fi.year_high - 1) * 100
            if closes is not None and s in closes and len(closes[s].dropna()) >= 6:
                c = closes[s].dropna()
                f.change_5d_pct = (f.price / float(c.iloc[-6]) - 1) * 100 if f.price else None
            if not f.is_fund:
                cal = t.calendar or {}
                dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
                if dates:
                    f.earnings = min(d for d in dates if isinstance(d, date))
        except Exception as e:  # noqa: BLE001 — one bad ticker must not sink the brief
            log.warning("Facts for %s incomplete: %s", s, e)
        out[s] = f
    return out


FEED_SOURCES = {"yahoo.com": "Yahoo Finance", "cnbc.com": "CNBC", "dowjones.io": "MarketWatch"}


def _default_source(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc
    return next((name for dom, name in FEED_SOURCES.items() if host.endswith(dom)), "")


def _parse_feed(url: str, max_age_h: float, now: float) -> list[tuple[str, str, str, float]]:
    import feedparser

    f = feedparser.parse(url, agent="Mozilla/5.0 nari-brief")
    rows = []
    for e in f.entries:
        ts, title = getattr(e, "published_parsed", None), getattr(e, "title", "").strip()
        if not ts or not title:
            continue
        age = (now - calendar.timegm(ts)) / 3600
        if age > max_age_h:
            continue
        src = getattr(getattr(e, "source", None), "title", "") or _default_source(url)
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3]
        rows.append((title, src, getattr(e, "link", ""), age))
    return rows


def clean_headlines(rows: list[tuple[str, str, str, float]], prefix: str, limit: int) -> list[Headline]:
    seen, out = set(), []
    for title, src, link, age in sorted(rows, key=lambda r: r[3]):
        key = re.sub(r"\W+", " ", title.lower()).strip()[:70]
        if key in seen or FLUFF_RE.search(title):
            continue
        seen.add(key)
        out.append(Headline(f"{prefix}{len(out) + 1}", title, src, link, round(age, 1)))
        if len(out) >= limit:
            break
    return out


def fetch_ticker_news(f: TickerFacts, max_age_h: float, limit: int, now: float,
                      aliases: list[str] | None = None) -> list[Headline]:
    names = " OR ".join(f'"{n}"' for n in dict.fromkeys([short_name(f.name), *(aliases or [])]))
    q = urllib.parse.quote(f'({names}) stock when:{max(1, round(max_age_h / 24))}d')
    rows = _parse_feed(GNEWS_SEARCH.format(q=q), max_age_h, now)
    rows += _parse_feed(YAHOO_TICKER_RSS.format(symbol=f.symbol), max_age_h, now)
    return clean_headlines(rows, f"{f.symbol}-", limit)


def fetch_market_news(feeds: list[str], max_age_h: float, limit: int, now: float) -> list[Headline]:
    rows = [r for url in feeds for r in _parse_feed(url, max_age_h, now)]
    return clean_headlines(rows, "M", limit)


def index_moves(symbols: dict[str, str]) -> list[tuple[str, float]]:
    import yfinance as yf

    out = []
    for sym, label in symbols.items():
        try:
            fi = yf.Ticker(sym).fast_info
            out.append((label, (fi.last_price / fi.previous_close - 1) * 100))
        except Exception as e:  # noqa: BLE001
            log.warning("Index %s failed: %s", sym, e)
    return out


# ---- LLM ------------------------------------------------------------------------

def _facts_line(f: TickerFacts, today: date) -> str:
    parts = [f"{f.symbol} ({f.name}{', fund' if f.is_fund else ''})"]
    if f.change_pct is not None:
        parts.append(f"today {f.change_pct:+.1f}%")
    if f.change_5d_pct is not None:
        parts.append(f"5d {f.change_5d_pct:+.1f}%")
    if f.volume_pace and f.volume_pace >= 1.5:
        parts.append(f"volume pace {f.volume_pace:.1f}x normal")
    if f.pct_from_52w_high is not None and f.pct_from_52w_high > -3:
        parts.append("near 52-week high")
    if f.earnings:
        parts.append(f"next earnings {f.earnings:%b %d} (in {(f.earnings - today).days} days)")
    return ", ".join(parts)


def build_prompt(portfolio: list[TickerFacts], watch: list[TickerFacts], market: list[Headline], today: date) -> str:
    def section(title: str, items: list[TickerFacts]) -> list[str]:
        lines = [f"## {title}"]
        for f in items:
            lines.append(_facts_line(f, today))
            lines += [f"  [{h.id}] {h.title} ({h.source or 'Yahoo'}, {h.age_h}h ago)" for h in f.headlines]
            if not f.headlines:
                lines.append("  (no fresh headlines)")
        return lines

    return "\n".join([
        "You prepare a concise morning brief for one individual investor, read ~2 hours after the US open.",
        "Goal: surface concrete developments that could matter for their stocks, so they know what to look into.",
        "",
        "RULES",
        "- Use ONLY the facts and headlines below. Never invent numbers, dates or events. Cite headline ids.",
        "- A development is concrete: earnings/guidance, product launch or sales data, deal/M&A/IPO, contract,",
        "  regulation/lawsuit, executive change, analyst rating change WITH a stated reason, or a named macro/sector",
        "  driver of today's move. Ignore listicles, generic 'stock rises' notes and opinion pieces.",
        "- Include a stock ONLY for a concrete, company-specific development. Set 'type' to one of: earnings, guidance,",
        "  product, sales_data, deal, ipo, contract, regulatory, legal, executive, analyst, other_specific.",
        "  If the only story is that it moved with the market/sector (e.g. 'fell amid tech sell-off'), DO NOT include",
        "  it; put it in quiet and describe the sector move once in market_stories instead.",
        "- 'what' must name the specific event (who did what), not restate the price move.",
        "- 'reaction' = how the market is taking it, judged from today's price move and the headlines.",
        "- 'watch' = a concrete upcoming event with its name, e.g. 'Earnings Oct 29', 'FDA decision in November',",
        "  or \"\" if none. Never a bare date.",
        "- No buy/sell/hold recommendations, no price targets of your own, no predictions of price direction.",
        "- market_stories: up to 4 of today's biggest market-wide stories (IPOs, M&A, macro data, Fed, sector",
        "  selloffs/rallies, major company news) from the Market headlines. Skip ones already covered per stock.",
        "- Plain text in all fields, no markdown. what <=20 words, why <=20 words, watch <=8 words.",
        "",
        'Return JSON: {"portfolio": [{"symbol", "type", "what", "why", "watch", "reaction": "positive|negative|mixed|neutral",',
        '"sources": [ids]}], "watchlist": [same shape], "market_stories": [{"what", "why", "sources": [ids]}],',
        '"quiet": [symbols]}',
        "",
        *section("Portfolio", portfolio),
        "",
        *section("Watchlist", watch),
        "",
        "## Market headlines",
        *[f"[{h.id}] {h.title} ({h.source or 'feed'}, {h.age_h}h ago)" for h in market],
    ])


def parse_llm(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Brief LLM returned non-JSON")
        return None
    return data if isinstance(data, dict) else None


# ---- formatting -----------------------------------------------------------------

def _esc(text: str) -> str:
    """Escape for Telegram HTML text (apostrophes/quotes stay readable)."""
    return html.escape(text, quote=False)


REACTION = {"positive": "🟢", "negative": "🔴", "mixed": "🟡", "neutral": "⚪"}
SPECIFIC_TYPES = {"earnings", "guidance", "product", "sales_data", "deal", "ipo", "contract", "regulatory", "legal",
                  "executive", "analyst", "other_specific"}
BARE_DATE_RE = re.compile(r"^(on )?([A-Z][a-z]{2,8}\.? \d{1,2}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2})$")


def name_terms(f: "TickerFacts", aliases: dict[str, list[str]] | None = None) -> list[str]:
    """Lower-case words that identify the company in a headline: symbol, distinctive name words, aliases."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z&'\-]+", short_name(f.name)) if len(w) >= 4]
    return [t.lower() for t in [f.symbol, *words[:2], *((aliases or {}).get(f.symbol, []))]]


def mentions(headline: str, terms: list[str]) -> bool:
    h = headline.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(t)}", h) for t in terms)


def keep_item(it: dict, f: "TickerFacts | None" = None, by_id: dict | None = None,
              aliases: dict[str, list[str]] | None = None) -> bool:
    """Drop 'moved with the market' items and items whose cited headlines aren't about this company
    (the model is told both rules; lite models don't always follow them)."""
    if str(it.get("type", "")).lower() not in SPECIFIC_TYPES or not str(it.get("what", "")).strip():
        return False
    if f is None or by_id is None:
        return True
    cited = [by_id[str(i)].title for i in it.get("sources") or [] if str(i) in by_id]
    terms = name_terms(f, aliases)
    return any(mentions(t, terms) for t in cited)


def fix_watch(watch: str, f: "TickerFacts | None") -> str:
    w = watch.strip()
    if BARE_DATE_RE.match(w):
        return f"Earnings {w}" if f and f.earnings else ""
    return w


def _pct(v: float | None) -> str:
    return f"{v:+.1f}%" if v is not None else "n/a"


def _price_line(f: TickerFacts, today: date) -> str:
    line = f"<b>{html.escape(f.symbol)}</b> {f.price:,.2f} ({_pct(f.change_pct)})" if f.price else f"<b>{f.symbol}</b>"
    if f.volume_pace and f.volume_pace >= 2:
        line += f" · vol {f.volume_pace:.1f}× pace"
    if f.earnings and 0 <= (f.earnings - today).days <= 14:
        line += f" · earnings {f.earnings:%b %d}"
    return line


def _sources(ids: list[str], by_id: dict[str, Headline]) -> str:
    links = []
    for i in ids[:3]:
        h = by_id.get(str(i))
        if h and h.link:
            links.append(f'<a href="{html.escape(h.link)}">{html.escape(h.source or "source", quote=False)}</a>')
    return " · ".join(links)


def _items(title: str, items: list[dict], facts: dict[str, TickerFacts], by_id: dict[str, Headline],
           today: date) -> list[str]:
    lines = []
    for it in items:
        f = facts.get(str(it.get("symbol", "")).upper())
        if not f:
            continue
        icon = REACTION.get(str(it.get("reaction", "")).lower(), "⚪")
        lines.append(f"{icon} {_price_line(f, today)} — {_esc(str(it.get('what', '')))}")
        tail = []
        if it.get("why"):
            tail.append(_esc(str(it["why"])))
        if watch := fix_watch(str(it.get("watch") or ""), f):
            tail.append(f"<i>Watch:</i> {_esc(watch)}")
        if src := _sources(it.get("sources") or [], by_id):
            tail.append(src)
        if tail:
            lines.append("   ↳ " + " · ".join(tail))
    return ([f"\n<b>{title}</b>"] + lines) if lines else []


def _quiet_line(symbols: list[str], facts: dict[str, TickerFacts]) -> str:
    return "<i>Quiet:</i> " + ", ".join(f"{s} {_pct(facts[s].change_pct)}" for s in symbols if s in facts)


def format_brief(now: datetime, indexes: list[tuple[str, float]], portfolio: list[str], watch: list[str],
                 facts: dict[str, TickerFacts], market: list[Headline], llm: dict[str, Any] | None,
                 aliases: dict[str, list[str]] | None = None) -> str:
    today = now.date()
    by_id = {h.id: h for f in facts.values() for h in f.headlines} | {h.id: h for h in market}
    out = [f"☀️ <b>Morning brief — {now:%a %b %d}</b> <i>(as of {now:%-I:%M%p %Z})</i>"]
    if indexes:
        out.append(" · ".join(f"{_esc(label)} {_pct(v)}" for label, v in indexes))

    if llm:
        for key, title, symbols in (("portfolio", "💼 Portfolio", portfolio), ("watchlist", "👀 Watchlist", watch)):
            items = [i for i in llm.get(key) or [] if str(i.get("symbol", "")).upper() in symbols
                     and keep_item(i, facts.get(str(i.get("symbol", "")).upper()), by_id, aliases)]
            out += _items(title, items, facts, by_id, today)
            covered = {str(i.get("symbol", "")).upper() for i in items}
            rest = [s for s in symbols if s not in covered]
            if rest:
                if not items:
                    out.append(f"\n<b>{title}</b>")
                out.append(_quiet_line(rest, facts))
        stories = llm.get("market_stories") or []
        if stories:
            out.append("\n<b>🌎 Market stories</b>")
            for st in [st for st in stories if str(st.get("what", "")).strip()][:4]:
                line = f"• {_esc(str(st['what']))}"
                if st.get("why"):
                    line += f" — {_esc(str(st['why']))}"
                if src := _sources(st.get("sources") or [], by_id):
                    line += f" · {src}"
                out.append(line)
    else:  # LLM unavailable: prices + top headline per stock
        for title, symbols in (("💼 Portfolio", portfolio), ("👀 Watchlist", watch)):
            if not symbols:
                continue
            out.append(f"\n<b>{title}</b>")
            for s in symbols:
                f = facts.get(s)
                if not f:
                    continue
                out.append(_price_line(f, today))
                for h in f.headlines[:2]:
                    out.append(f'  • <a href="{html.escape(h.link)}">{_esc(h.title)}</a>')
        if market:
            out.append("\n<b>🌎 Headlines</b>")
            out += [f'• <a href="{html.escape(h.link)}">{_esc(h.title)}</a>' for h in market[:6]]
        out.append("\n<i>(AI summary unavailable today; showing raw headlines.)</i>")

    upcoming = sorted((f.earnings, s) for s, f in facts.items()
                      if f.earnings and 0 <= (f.earnings - today).days <= 14 and s in portfolio + watch)
    if upcoming:
        out.append("\n📅 <b>Earnings next 2 weeks:</b> " + ", ".join(f"{s} {d:%b %d}" for d, s in upcoming))
    if not portfolio and not watch:
        out.append("\nAdd stocks with /portfolio add IBM KO or /watch add XOM")
    out.append("\n<i>Informational only — not financial advice.</i>")
    return "\n".join(out)


# ---- orchestration --------------------------------------------------------------

def last_trading_day() -> date | None:
    import yfinance as yf

    df = yf.download("SPY", period="5d", progress=False, auto_adjust=True)
    return df.index[-1].date() if len(df) else None


class BriefBuilder:
    """Gathers data once per run and builds one brief per person (lists differ, data is shared)."""

    def __init__(self, settings: dict[str, Any], summarize: Callable[[str], str | None] | None,
                 tz: ZoneInfo, name_cache: dict[str, tuple[str, str]] | None = None):
        self.m = settings["market"]
        self.aliases: dict[str, list[str]] = {k.upper(): list(v) for k, v in (self.m.get("aliases") or {}).items()}
        self.summarize = summarize
        self.tz = tz
        self.name_cache = name_cache if name_cache is not None else {}
        self.facts: dict[str, TickerFacts] = {}
        self.market: list[Headline] | None = None
        self.indexes: list[tuple[str, float]] | None = None
        self.econ: list[Event] | None = None
        self.ipos: list[Event] | None = None
        self.timing: dict[str, str] = {}
        self.timing_checked: set[str] = set()

    def _ensure(self, symbols: list[str]) -> None:
        now_ts, now_ny = time.time(), datetime.now(NY)
        missing = [s for s in symbols if s not in self.facts]
        if missing:
            news = self.m.get("news", {})
            for s, f in fetch_facts(missing, now_ny, self.name_cache).items():
                if not f.is_fund or news.get("include_funds"):
                    f.headlines = fetch_ticker_news(f, news.get("max_age_hours", 30), news.get("per_ticker", 8),
                                                    now_ts, self.aliases.get(s))
                self.facts[s] = f
        if self.market is None:
            news = self.m.get("news", {})
            self.market = fetch_market_news(self.m["news_feeds"], news.get("max_age_hours", 30),
                                            self.m.get("max_headlines", 25), now_ts)
        if self.indexes is None:
            self.indexes = index_moves(self.m.get("indexes", {}))

    def _ensure_ahead(self, symbols: list[str]) -> None:
        days = self.m.get("ahead_days", 14)
        if self.econ is None:
            self.econ = fetch_economic_events(days)
            self.ipos = fetch_ipos(days)
        missing = [s for s in symbols if s not in self.timing_checked]
        if missing:
            self.timing.update(fetch_earnings_timing(missing, days))
            self.timing_checked.update(missing)

    def build_evening(self, portfolio: list[str], watch: list[str]) -> str:
        symbols = list(dict.fromkeys(portfolio + watch))
        self._ensure(symbols)
        self._ensure_ahead(symbols)
        now, days = datetime.now(self.tz), self.m.get("ahead_days", 14)
        today = now.date()
        ahead = [f"- {_when(e.when)}: US {e.what} ({e.detail})" for e in self.econ or []]
        ahead += [f"- {f.earnings:%a %b %-d}: {s} earnings" + (f" ({self.timing[s]})" if self.timing.get(s) else "")
                  for s, f in self.facts.items() if s in symbols and f.earnings and 0 <= (f.earnings - today).days <= days]
        ahead += [f"- {e.when:%a %b %-d}: IPO {e.what}" for e in self.ipos or []]
        llm_out = None
        if self.summarize:
            has_news = lambda s: bool(self.facts[s].headlines)  # noqa: E731
            prompt = build_evening_prompt([self.facts[s] for s in portfolio if has_news(s)],
                                          [self.facts[s] for s in watch if has_news(s)], self.market or [], ahead, today)
            llm_out = parse_llm(self.summarize(prompt))
        return format_evening(now, self.indexes or [], portfolio, watch, self.facts, self.market or [], llm_out,
                              self.econ or [], self.ipos or [], self.timing, days, self.aliases)

    def build(self, portfolio: list[str], watch: list[str], mode: str = "morning") -> str:
        if mode == "evening":
            return self.build_evening(portfolio, watch)
        self._ensure(list(dict.fromkeys(portfolio + watch)))
        now = datetime.now(self.tz)
        llm_out = None
        if self.summarize:
            # Funds/stocks with no fresh headlines can't be explained; they go straight to "quiet"
            # instead of costing prompt tokens.
            has_news = lambda s: bool(self.facts[s].headlines)  # noqa: E731
            prompt = build_prompt([self.facts[s] for s in portfolio if has_news(s)],
                                  [self.facts[s] for s in watch if has_news(s)], self.market or [], now.date())
            llm_out = parse_llm(self.summarize(prompt))
        return format_brief(now, self.indexes or [], portfolio, watch, self.facts, self.market or [], llm_out, self.aliases)


def validate_symbols(symbols: list[str]) -> tuple[list[str], list[str]]:
    """Return (valid, invalid) using Yahoo quotes."""
    import yfinance as yf

    valid, invalid = [], []
    for s in symbols:
        try:
            price = yf.Ticker(s).fast_info.last_price
            (valid if price else invalid).append(s)
        except Exception:  # noqa: BLE001
            invalid.append(s)
    return valid, invalid


# ---- evening: close + look ahead ------------------------------------------------------------

# Market-moving US releases (Yahoo event names vary slightly; match loosely).
KEY_US_EVENTS = re.compile(
    r"\bCPI\b|\bPPI\b|Non-?Farm Payroll|Unemployment Rate|\bPCE\b|\bGDP\b|Retail Sales|Fed Funds|FOMC|"
    r"Initial Jobless|ISM (Manufacturing PMI|N-Mfg PMI|Services PMI)|Consumer Confidence|U Mich Sentiment|"
    r"JOLTS|Durable Goods, R MM|Housing Starts|Existing Home Sales|New Home Sales", re.I)
EVENT_NAMES = [  # (pattern, plain name, tier-1?)
    (r"Fed Funds|FOMC", "Fed rate decision", True), (r"Non-?Farm|Unemployment Rate", "Jobs report", True),
    (r"\bCPI\b", "CPI inflation", True), (r"\bPCE\b", "PCE inflation", True), (r"\bGDP\b", "GDP", True),
    (r"\bPPI\b", "PPI", False), (r"Retail Sales", "Retail sales", False), (r"Jobless", "Jobless claims", False),
    (r"ISM Manufacturing", "ISM manufacturing", False), (r"ISM (N-Mfg|Services)", "ISM services", False),
    (r"Consumer Confidence", "Consumer confidence", False), (r"U Mich", "Michigan sentiment", False),
    (r"JOLTS", "Job openings (JOLTS)", False), (r"Durable", "Durable goods", False),
    (r"Housing Starts", "Housing starts", False), (r"Home Sales", "Home sales", False),
]


def plain_event(name: str) -> tuple[str, bool]:
    for pat, label, tier1 in EVENT_NAMES:
        if re.search(pat, name, re.I):
            return label, tier1
    return name, False


def group_events(events: list[Event], max_days: int = 6) -> list[tuple[date, list[str]]]:
    """[(day, ['⭐ Fed rate decision', 'PPI', ...])]; days with a tier-1 release are always kept."""
    days: dict[date, list[tuple[bool, str]]] = {}
    for e in events:
        d = e.when.date() if isinstance(e.when, datetime) else e.when
        label, tier1 = plain_event(e.what)
        if all(l != label for _, l in days.get(d, [])):
            days.setdefault(d, []).append((tier1, label))
    ordered = sorted(days.items())
    keep = {d for d, items in ordered if any(t for t, _ in items)}
    for d, _ in ordered:
        if len(keep) >= max_days:
            break
        keep.add(d)
    return [(d, [("⭐ " if t else "") + l for t, l in sorted(items, key=lambda x: not x[0])])
            for d, items in ordered if d in keep]


IPO_NOISE = re.compile(r"acquisition|spac|\bunits?\b|\betf\b|\btrust\b|\bfund\b|warrant|rights", re.I)


@dataclass
class Event:
    when: datetime | date
    what: str
    detail: str = ""


def fetch_economic_events(days: int, pattern: re.Pattern = KEY_US_EVENTS) -> list[Event]:
    """Key US releases for the next `days` days. Yahoo returns <=100 rows per query counted from the
    END of the window, so we query one day at a time and page through busy days."""
    import yfinance as yf
    from datetime import timedelta

    out, start = [], date.today()
    for d in range(1, days + 1):
        day = start + timedelta(days=d)
        cal, offset = yf.Calendars(start=day, end=day + timedelta(days=1)), 0
        while True:
            try:
                df = cal.get_economic_events_calendar(limit=100, offset=offset)
            except Exception as e:  # noqa: BLE001
                log.warning("Economic calendar %s failed: %s", day, e)
                break
            if df is None or not len(df):
                break
            for name, row in df[df["Region"] == "US"].iterrows():
                if pattern.search(str(name)):
                    label = str(name).replace("*", "").strip()
                    last = row.get("Last")
                    detail = f"for {row.get('For')}" + (f", last {last:g}" if isinstance(last, (int, float)) and last == last else "")
                    out.append(Event(row["Event Time"].tz_convert("America/Los_Angeles").to_pydatetime(), label, detail))
            if len(df) < 100 or offset >= 400:
                break
            offset += 100
    seen, uniq = set(), []
    for e in sorted(out, key=lambda e: e.when):            # same release often listed several times
        key = (e.when.date() if isinstance(e.when, datetime) else e.when, re.sub(r"\W+", "", e.what.lower())[:14])
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq


def fetch_earnings_timing(symbols: list[str], days: int) -> dict[str, str]:
    """symbol -> 'before open' / 'after close' where Yahoo's earnings calendar knows it."""
    import yfinance as yf
    from datetime import timedelta

    timing = {"BMO": "before open", "AMC": "after close"}
    try:
        df = yf.Calendars(start=date.today(), end=date.today() + timedelta(days=days)) \
            .get_earnings_calendar(filter_most_active=False, limit=100)
    except Exception as e:  # noqa: BLE001
        log.warning("Earnings calendar failed: %s", e)
        return {}
    return {s: timing.get(str(df.loc[s, "Timing"] if not hasattr(df.loc[s, "Timing"], "iloc") else df.loc[s, "Timing"].iloc[0]), "")
            for s in symbols if s in df.index}


def fetch_ipos(days: int, limit: int = 6) -> list[Event]:
    import yfinance as yf
    from datetime import timedelta

    try:
        df = yf.Calendars(start=date.today(), end=date.today() + timedelta(days=days)).get_ipo_info_calendar(limit=100)
    except Exception as e:  # noqa: BLE001
        log.warning("IPO calendar failed: %s", e)
        return []
    out, seen = [], set()
    for sym, row in df.iterrows():
        name = str(row.get("Company") or "")
        if sym in seen or IPO_NOISE.search(name) or str(row.get("Exchange")) not in ("NYSE", "Nasdaq", "NASDAQ"):
            continue
        seen.add(sym)
        name = re.sub(r"\s+(Common Stock|Class [A-C] Common Stock|Ordinary Shares|Common Shares)$", "", name)
        if row["Date"].date() > date.today():
            out.append(Event(row["Date"].date(), f"{name} ({sym})", str(row.get("Exchange"))))
    return sorted(out, key=lambda e: e.when)[:limit]


def build_evening_prompt(portfolio: list[TickerFacts], watch: list[TickerFacts], market: list[Headline],
                         ahead: list[str], today: date) -> str:
    def section(title: str, items: list[TickerFacts]) -> list[str]:
        lines = [f"## {title}"]
        for f in items:
            lines.append(_facts_line(f, today))
            lines += [f"  [{h.id}] {h.title} ({h.source or 'Yahoo'}, {h.age_h}h ago)" for h in f.headlines]
        return lines

    return "\n".join([
        "You write a short after-close note for one individual investor: how today went, and what's coming.",
        "",
        "RULES",
        "- Use ONLY the facts, headlines and calendar below. Never invent numbers, dates or events. Cite headline ids.",
        "- wrap: 2-3 bullets on what drove the US market today (macro data, Fed, sector moves, big company news).",
        "- portfolio / watchlist: ONLY stocks with a concrete company-specific development today (types: earnings,",
        "  guidance, product, sales_data, deal, ipo, contract, regulatory, legal, executive, analyst, other_specific).",
        "  'Moved with the market' is not a development — leave it out.",
        "- ahead: up to 4 upcoming items from the CALENDAR or headlines that matter for THIS person's stocks or the",
        "  broad market. 'when' = date from the calendar; 'why' = why it matters for their holdings, factually.",
        "- No buy/sell/hold recommendations, no price targets of your own, no predictions of price direction.",
        "- Plain text, no markdown. what <=20 words, why <=20 words.",
        "",
        'Return JSON: {"wrap": [{"what", "why", "sources": [ids]}], "portfolio": [{"symbol", "type", "what", "why",',
        '"reaction": "positive|negative|mixed|neutral", "sources": [ids]}], "watchlist": [same], "ahead": [{"when", "what", "why"}]}',
        "",
        *section("Portfolio (today's close)", portfolio), "",
        *section("Watchlist (today's close)", watch), "",
        "## Market headlines", *[f"[{h.id}] {h.title} ({h.source or 'feed'}, {h.age_h}h ago)" for h in market], "",
        "## CALENDAR (next 2 weeks)", *ahead,
    ])


def _when(w: datetime | date) -> str:
    return f"{w:%a %b %-d %-I:%M%p}".replace(":00", "") if isinstance(w, datetime) else f"{w:%a %b %-d}"


def format_evening(now: datetime, indexes: list[tuple[str, float]], portfolio: list[str], watch: list[str],
                   facts: dict[str, TickerFacts], market: list[Headline], llm: dict[str, Any] | None,
                   econ: list[Event], ipos: list[Event], timing: dict[str, str], ahead_days: int,
                   aliases: dict[str, list[str]] | None = None) -> str:
    today = now.date()
    by_id = {h.id: h for f in facts.values() for h in f.headlines} | {h.id: h for h in market}
    out = [f"🌙 <b>Market close — {now:%a %b %d}</b>"]
    if indexes:
        out.append(" · ".join(f"{_esc(label)} {_pct(v)}" for label, v in indexes))
    for title, symbols in (("💼 Portfolio", portfolio), ("👀 Watchlist", watch)):
        if symbols:
            out.append(f"\n<b>{title} today:</b> " + ", ".join(
                f"{s} {_pct(facts[s].change_pct)}" for s in sorted(symbols, key=lambda s: -(abs(facts[s].change_pct or 0)))
                if s in facts))
    if llm:
        for key, title, symbols, cap in (("portfolio", "💼 What moved yours", portfolio, 5),
                                         ("watchlist", "👀 Watchlist news", watch, 4)):
            items = [i for i in llm.get(key) or [] if str(i.get("symbol", "")).upper() in symbols
                     and keep_item(i, facts.get(str(i.get("symbol", "")).upper()), by_id, aliases)]
            # Evening is a recap: keep the biggest movers with real news, so it stays ~one message.
            items.sort(key=lambda i: -abs(facts[str(i["symbol"]).upper()].change_pct or 0))
            out += _items(title, items[:cap], facts, by_id, today)
        if wrap := [w for w in llm.get("wrap") or [] if str(w.get("what", "")).strip()][:3]:
            out.append("\n<b>🌎 Today's story</b>")
            for w in wrap:
                line = f"• {_esc(str(w['what']))}"
                if w.get("why"):
                    line += f" — {_esc(str(w['why']))}"
                if src := _sources(w.get("sources") or [], by_id):
                    line += f" · {src}"
                out.append(line)
    else:
        out.append("\n<i>(AI summary unavailable today.)</i>")

    out.append(f"\n🔭 <b>Coming up (next {ahead_days} days)</b>")
    if llm and (ahead := [a for a in llm.get("ahead") or [] if str(a.get("what", "")).strip()][:4]):
        for a in ahead:
            out.append(f"• <b>{_esc(str(a.get('when', '')))}</b> {_esc(str(a['what']))}"
                       + (f" — {_esc(str(a['why']))}" if a.get("why") else ""))
    yours = sorted((f.earnings, s) for s, f in facts.items()
                   if f.earnings and 0 <= (f.earnings - today).days <= ahead_days and s in portfolio + watch)
    if yours:
        out.append("📅 <i>Your earnings:</i> " + ", ".join(
            f"{s} {d:%b %-d}" + (f" ({timing[s]})" if timing.get(s) else "") for d, s in yours))
    if econ:
        out.append("🏛️ <i>Key US data:</i> " + " · ".join(
            f"{d:%a %b %-d}: {_esc(', '.join(items))}" for d, items in group_events(econ)))
    if ipos:
        out.append("🔔 <i>IPOs:</i> " + "; ".join(f"{e.when:%b %-d} {_esc(e.what)}" for e in ipos))
    out.append("\n<i>Informational only — not financial advice.</i>")
    return "\n".join(out)
