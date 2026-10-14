"""Module C: US market close digest — watchlist, top movers, volume spikes, headlines, catalysts."""
from __future__ import annotations

import calendar
import html
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.modules import llm

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
YAHOO_TICKER_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbols}&region=US&lang=en-US"


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: float
    change_pct: float
    volume: int | None = None
    avg_volume: int | None = None

    @property
    def volume_ratio(self) -> float | None:
        if self.volume and self.avg_volume:
            return self.volume / self.avg_volume
        return None


# ---- data fetching (yfinance / RSS) -------------------------------------------

def last_trading_day() -> date | None:
    import yfinance as yf

    df = yf.download("SPY", period="5d", progress=False, auto_adjust=True)
    return df.index[-1].date() if len(df) else None


def watchlist_quotes(symbols: list[str]) -> list[Quote]:
    import yfinance as yf

    df = yf.download(symbols, period="5d", progress=False, auto_adjust=True)["Close"]
    out = []
    for s in symbols:
        col = df[s].dropna() if s in df else []
        if len(col) < 2:
            continue
        last, prev = float(col.iloc[-1]), float(col.iloc[-2])
        out.append(Quote(s, s, last, (last / prev - 1) * 100))
    return out


def _screen(name: str, count: int) -> list[dict[str, Any]]:
    import yfinance as yf

    try:
        return yf.screen(name, count=count).get("quotes", [])
    except Exception as e:  # noqa: BLE001
        log.warning("yfinance screen %s failed: %s", name, e)
        return []


def quote_from_screen(q: dict[str, Any]) -> Quote:
    return Quote(
        symbol=q.get("symbol", "?"),
        name=q.get("shortName") or q.get("longName") or q.get("symbol", "?"),
        price=float(q.get("regularMarketPrice") or 0),
        change_pct=float(q.get("regularMarketChangePercent") or 0),
        volume=q.get("regularMarketVolume"),
        avg_volume=q.get("averageDailyVolume3Month"),
    )


def movers(min_cap: float, count: int, spike_ratio: float) -> tuple[list[Quote], list[Quote], list[Quote]]:
    """Return (gainers, losers, volume spikes), filtered to market cap >= min_cap."""
    def big(rows: list[dict[str, Any]]) -> list[Quote]:
        return [quote_from_screen(r) for r in rows if (r.get("marketCap") or 0) >= min_cap]

    gainers = big(_screen("day_gainers", 100))[:count]
    losers = big(_screen("day_losers", 100))[:count]
    pool = {q.symbol: q for q in big(_screen("most_actives", 100)) + gainers + losers}
    spikes = sorted((q for q in pool.values() if (q.volume_ratio or 0) >= spike_ratio),
                    key=lambda q: q.volume_ratio or 0, reverse=True)[:count]
    return gainers, losers, spikes


def fresh_titles(entries: list[Any], max_age_hours: float, now: float | None = None) -> list[str]:
    """Titles of entries published within max_age_hours, newest first. Undated entries are dropped."""
    now = now if now is not None else time.time()
    dated = []
    for e in entries:
        title, ts = getattr(e, "title", "").strip(), getattr(e, "published_parsed", None)
        if title and ts and now - calendar.timegm(ts) <= max_age_hours * 3600:
            dated.append((calendar.timegm(ts), title))
    return [t for _, t in sorted(dated, reverse=True)]


def headlines(feeds: list[str], limit: int, max_age_hours: float = 30) -> list[str]:
    import feedparser

    out: list[str] = []
    for url in feeds:
        f = feedparser.parse(url, agent="Mozilla/5.0 nari-digest")
        if not f.entries:
            log.warning("News feed returned nothing: %s", url)
        out += fresh_titles(f.entries, max_age_hours)
    return list(dict.fromkeys(out))[:limit]


def ticker_headlines(symbols: list[str], per_symbol: int = 3, max_age_hours: float = 30) -> dict[str, list[str]]:
    import feedparser

    result: dict[str, list[str]] = {}
    for s in symbols:
        f = feedparser.parse(YAHOO_TICKER_RSS.format(symbols=s), agent="Mozilla/5.0 nari-digest")
        result[s] = fresh_titles(f.entries, max_age_hours)[:per_symbol]
    return result


# ---- formatting ----------------------------------------------------------------

def _q(q: Quote, with_volume: bool = False) -> str:
    arrow = "🟢" if q.change_pct >= 0 else "🔴"
    line = f"{arrow} <b>{html.escape(q.symbol)}</b> {q.price:,.2f} ({q.change_pct:+.2f}%)"
    if q.name and q.name != q.symbol:
        line += f" {html.escape(q.name[:28])}"
    if with_volume and q.volume_ratio:
        line += f" · vol {q.volume_ratio:.1f}× avg"
    return line


def build_catalyst_prompt(movers_: list[Quote], news: dict[str, list[str]], market_news: list[str]) -> str:
    lines = [
        "You write a short after-close US stock market note for two people. Rules:",
        "- Use ONLY the headlines below. If a stock's headlines don't explain the move, say 'no clear catalyst in headlines'.",
        "- Never give buy/sell/hold advice, price targets, or predictions.",
        "- Plain text only, no markdown. One line per stock as 'SYMBOL: catalyst (<=15 words)'.",
        "- Then one final line starting 'Market:' summarizing the broad market story in <=30 words.",
        "",
        "Stocks and their headlines:",
    ]
    for q in movers_:
        lines.append(f"{q.symbol} ({q.change_pct:+.1f}%): " + " | ".join(news.get(q.symbol, [])[:3]))
    lines += ["", "Market headlines:"] + [f"- {h}" for h in market_news]
    return "\n".join(lines)


def format_digest(day: date, watch: list[Quote], gainers: list[Quote], losers: list[Quote],
                  spikes: list[Quote], catalysts: str | None, market_news: list[str]) -> str:
    out = [f"📈 <b>Market close — {day:%a %b %d, %Y}</b>"]
    if watch:
        out += ["", "<b>Watchlist</b>"] + [_q(q) for q in watch]
    if gainers:
        out += ["", "<b>Top gainers</b>"] + [_q(q) for q in gainers]
    if losers:
        out += ["", "<b>Top losers</b>"] + [_q(q) for q in losers]
    if spikes:
        out += ["", "<b>Volume spikes</b>"] + [_q(q, with_volume=True) for q in spikes]
    if catalysts:
        out += ["", "<b>Key catalysts</b> <i>(AI summary of headlines)</i>", html.escape(catalysts)]
    elif market_news:
        out += ["", "<b>Headlines</b>"] + [f"• {html.escape(h)}" for h in market_news[:8]]
    out += ["", "<i>Informational only — not financial advice.</i>"]
    return "\n".join(out)


def build_digest(settings: dict[str, Any], gemini_key: str, force: bool = False) -> str | None:
    """Return the digest, or None if the market didn't trade today (holiday)."""
    m = settings["market"]
    today = datetime.now(NY).date()
    traded = last_trading_day()
    if not force and traded != today:
        log.info("No trading session today (last=%s); skipping digest", traded)
        return None
    watch = watchlist_quotes(m["watchlist"])
    gainers, losers, spikes = movers(m["min_market_cap"], m["movers_count"], m["volume_spike_ratio"])
    max_age = m.get("max_headline_age_hours", 30)
    market_news = headlines(m["news_feeds"], m["max_headlines"], max_age)
    catalysts = None
    focus = list({q.symbol: q for q in gainers + losers + spikes}.values())
    if m.get("llm", {}).get("enabled") and focus:
        news = ticker_headlines([q.symbol for q in focus], max_age_hours=max_age)
        catalysts = llm.summarize(gemini_key, settings["llm"]["models"],
                                  build_catalyst_prompt(focus, news, market_news))
    return format_digest(traded or today, watch, gainers, losers, spikes, catalysts, market_news)
