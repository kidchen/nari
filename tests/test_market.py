"""Market tests."""
from datetime import date

import pytest
from helpers import (
    _facts,
)

from app.db import DB
from app.features.market import brief


def test_clean_headlines_filters_fluff_and_dupes():
    rows = [("What $10,000 Invested in Acme Today Could Be Worth", "Motley Fool", "l1", 1.0),
            ("Acme Stock Is Down. It's CoreWeave's Death Spiral.", "Barron's", "l2", 1.7),
            ("Acme stock is down. It\u2019s CoreWeave\u2019s death spiral", "Yahoo", "l3", 2.0),
            ("Acme (NASDAQ:ACME) Stock Falls 2.8% - Should You Sell?", "MarketBeat", "l4", 0.2),
            ("Acme wins $2B NASA contract", "Reuters", "l5", 3.0)]
    out = brief.clean_headlines(rows, "ACME-", 10)
    assert [h.title for h in out] == ["Acme Stock Is Down. It's CoreWeave's Death Spiral.", "Acme wins $2B NASA contract"]
    assert [h.id for h in out] == ["ACME-1", "ACME-2"]


def test_short_name_and_session_fraction():
    from datetime import datetime as dt
    assert brief.short_name("Fruit Computer Inc.") == "Fruit Computer"
    assert brief.short_name("Widget Technologies Inc.") == "Widget"
    assert brief.short_name("Acme Holdings, Inc.") == "Acme"
    assert brief.session_fraction(dt(2026, 10, 14, 11, 30, tzinfo=brief.NY)) == pytest.approx(120 / 390)
    assert brief.session_fraction(dt(2026, 10, 14, 8, 0, tzinfo=brief.NY)) == 0.05


def test_prompt_has_rules_facts_and_ids():
    f = _facts()
    p = brief.build_prompt([f["ACME"], f["IDXF"]], [f["FRUT"]],
                           [brief.Headline("M1", "OpenAI files for IPO", "Reuters", "l", 2.0)], date(2026, 10, 14))
    assert "No buy/sell/hold" in p and "[ACME-1] Acme stock is down" in p and "[M1] OpenAI files for IPO" in p
    assert "next earnings Nov 03 (in 20 days)" in p and "volume pace 2.4x normal" in p and "fund" in p


def test_format_brief_with_llm():
    from datetime import datetime as dt
    f = _facts()
    llm_out = brief.parse_llm('''```json
{"portfolio": [{"symbol": "ACME", "type": "deal", "what": "Shares fall with CoreWeave <AI debt> worries", "why": "Investors link the two",
  "watch": "Nov 3", "reaction": "negative", "sources": ["ACME-1"]},
  {"symbol": "IDXF", "type": "none", "what": "Fell with the market", "why": "", "watch": "", "reaction": "negative", "sources": []}],
 "watchlist": [], "market_stories": [{"what": "OpenAI files for IPO", "why": "Could be the largest ever", "sources": ["M1"]}],
 "quiet": ["IDXF", "FRUT"]}
```''')
    market = [brief.Headline("M1", "OpenAI files for IPO", "Reuters", "https://r/1", 2.0)]
    out = brief.format_brief(dt(2026, 10, 14, 8, 30, tzinfo=brief.ZoneInfo("America/Los_Angeles")),
                             [("S&P 500", -1.0)], ["ACME", "IDXF"], ["FRUT"], f, market, llm_out)
    assert "🔴 <b>ACME</b> 152.16 (-2.8%) · vol 2.4× pace — Shares fall" in out  # Nov 3 is 20d out: no earnings tag
    assert "<i>Watch:</i> Earnings Nov 3" in out          # bare date fixed up
    assert "Fell with the market" not in out              # type none dropped -> IDXF goes to quiet
    assert "&lt;AI debt&gt;" in out and '<a href="https://b/1">Barron\'s</a>' in out
    assert "<i>Quiet:</i> IDXF -1.0%" in out and "<i>Quiet:</i> FRUT +0.5%" in out
    assert "OpenAI files for IPO" in out and '<a href="https://r/1">Reuters</a>' in out
    assert "Earnings next 2 weeks" not in out or "ACME" not in out.split("Earnings next 2 weeks")[1]  # Nov 3 is 20d out
    assert "not financial advice" in out


def test_format_brief_fallback_without_llm():
    from datetime import datetime as dt
    out = brief.format_brief(dt(2026, 10, 14, 8, 30, tzinfo=brief.ZoneInfo("America/Los_Angeles")),
                             [], ["ACME"], [], _facts(), [], None)
    assert "Acme stock is down" in out and "AI summary unavailable" in out
    assert brief.parse_llm("not json") is None


def test_ticker_lists_db():
    db = DB(":memory:")
    assert db.add_tickers("p", "portfolio", ["SHOP", "CHIP"]) == ["SHOP", "CHIP"]
    assert db.add_tickers("p", "portfolio", ["CHIP", "IDXF"]) == ["IDXF"]
    assert db.remove_tickers("p", "portfolio", ["CHIP", "XYZ"]) == ["CHIP"]
    assert db.tickers("p", "portfolio") == ["SHOP", "IDXF"] and db.tickers("p", "watch") == []
    assert db.has_any_tickers("p") and not db.has_any_tickers("q")


def test_keep_item_and_fix_watch():
    assert brief.keep_item({"type": "analyst", "what": "Downgraded by MS on ad slowdown"})
    assert not brief.keep_item({"type": "none", "what": "Fell with tech"})
    assert not brief.keep_item({"type": "earnings", "what": "  "})
    f = brief.TickerFacts("X", earnings=date(2026, 10, 29))
    assert brief.fix_watch("Oct 29", f) == "Earnings Oct 29" and brief.fix_watch("Oct 29", brief.TickerFacts("Y")) == ""
    assert brief.fix_watch("FDA decision in November", f) == "FDA decision in November"


def test_relevance_check_drops_headlines_about_other_companies():
    f = brief.TickerFacts("SHOP", "Big River Retail, Inc.", "EQUITY", 100.0, -1.7)
    g = brief.TickerFacts("ABCD", "Alphabetica Inc.", "EQUITY", 100.0, -1.7)
    by_id = {"SHOP-1": brief.Headline("SHOP-1", "Kroger makes pivotal decision as customers turn to rivals", "Y", "l", 1),
             "SHOP-2": brief.Headline("SHOP-2", "Big River wins record holiday orders", "Y", "l", 1),
             "ABCD-1": brief.Headline("ABCD-1", "Xero integrates Google Gemini into its platform", "Y", "l", 1)}
    item = lambda src: {"type": "deal", "what": "x", "sources": [src]}  # noqa: E731
    assert not brief.keep_item(item("SHOP-1"), f, by_id)                      # Kroger story under SHOP: dropped
    assert brief.keep_item(item("SHOP-2"), f, by_id)
    assert not brief.keep_item(item("ABCD-1"), g, by_id)                      # needs the alias ...
    assert brief.keep_item(item("ABCD-1"), g, by_id, {"ABCD": ["Google"]})   # ... and then it's kept
    assert brief.keep_item({"type": "deal", "what": "x"})                     # no facts given: type check only


def test_group_events_plain_names_and_tier1_always_kept():
    from datetime import datetime as dt
    from datetime import timedelta
    base = dt(2026, 10, 15, 5, 30)
    ev = [brief.Event(base, "PPI Final Demand YY"), brief.Event(base, "PPI exFood/Energy YY"),
          brief.Event(base, "Retail Sales MM"), brief.Event(base, "Initial Jobless Clm")]
    ev += [brief.Event(base + timedelta(days=d), "Housing Starts Number") for d in range(1, 9)]
    ev.append(brief.Event(dt(2026, 10, 28, 11, 0), "Fed Funds Tgt Rate"))
    groups = brief.group_events(ev, max_days=3)
    assert groups[0] == (base.date(), ["PPI", "Retail sales", "Jobless claims"])
    assert groups[-1] == (dt(2026, 10, 28).date(), ["⭐ Fed rate decision"]) and len(groups) == 3


def test_format_evening_sections():
    from datetime import datetime as dt
    f = _facts()
    llm_out = {"wrap": [{"what": "Hot inflation data hit tech", "why": "Rate worries", "sources": ["M1"]}],
               "portfolio": [{"symbol": "ACME", "type": "deal", "what": "Acme stock is down", "why": "Contagion",
                              "reaction": "negative", "sources": ["ACME-1"]}], "watchlist": [],
               "ahead": [{"when": "Oct 28", "what": "Fed rate decision", "why": "Rates drive tech valuations"}]}
    market = [brief.Headline("M1", "Nasdaq falls on hot CPI", "Reuters", "https://r/1", 2.0)]
    econ = [brief.Event(dt(2026, 10, 28, 11), "Fed Funds Tgt Rate")]
    out = brief.format_evening(dt(2026, 10, 14, 16, 30, tzinfo=brief.ZoneInfo("America/Los_Angeles")), [("S&P 500", -1.0)],
                               ["ACME", "IDXF"], ["FRUT"], f, market, llm_out, econ,
                               [brief.Event(date(2026, 10, 27), "Example Corp (EXMP)")], {"FRUT": "after close"}, 14)
    assert "🌙 <b>Market close — Wed Oct 14</b>" in out and "<b>💼 Portfolio today:</b> ACME -2.8%, IDXF -1.0%" in out
    assert "Acme stock is down" in out and "Hot inflation data hit tech" in out
    assert "<b>Oct 28</b> Fed rate decision — Rates drive tech valuations" in out
    assert "Wed Oct 28: ⭐ Fed rate decision" in out and "Oct 27 Example Corp (EXMP)" in out
