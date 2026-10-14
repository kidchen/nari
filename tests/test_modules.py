from datetime import date
from pathlib import Path

import pytest

from app.db import DB
from app.modules import brief, cards, mortgage, placeholder, realestate, redfin_email
from app.notify import split_message

FIXTURES = Path(__file__).parent / "fixtures"
ZIPS = {"94607": "Oakland", "94704": "Berkeley", "94103": "San Francisco"}


def _card(listing_id, price, addr, city_line, was=None, beds=3, baths=2, sqft=1500):
    was_html = f'<span style="text-decoration: line-through;">${was:,}</span>' if was else ""
    return (f'<a href="https://redmail3.redfin.com/a/click?_e=x{listing_id}"><img src="https://www.redfin.com/'
            f'stingray/do/api-get-listing-hero-shot/{listing_id}/heroshot.jpg"></a>'
            f'<h3><a>${price:,} {was_html}</a></h3><p><a>{beds} Beds {baths} Baths {sqft:,} Sq Ft</a></p>'
            f'<p><a href="https://redmail3.redfin.com/a/click?_e=addr{listing_id}">{addr}</a><br>'
            f'<a href="https://redmail3.redfin.com/a/click?_e=city{listing_id}">{city_line}</a></p>')


def test_parse_real_sold_email_fixture():
    html_ = (FIXTURES / "redfin_sold.html").read_text()
    cards = redfin_email.parse_email("2 homes like 123 Example Street just sold.", html_)
    assert [(c.key, c.price, c.was_price, c.beds, c.baths, c.sqft, c.zip, c.kind) for c in cards] == [
        ("redfin:100000001", 700000, 710000, 2, 2.0, 1000, "94103", "sold"),
        ("redfin:100000002", 600000, 590000, 2, 2.0, 1100, "94704", "sold"),
    ]
    assert cards[0].address == "100 Sample Street Apartment 1" and cards[0].city == "San Francisco"
    assert cards[0].url.startswith("https://redmail3.redfin.com/a/click")
    # both are condos ("Apartment") -> dropped by the single-family filter
    assert realestate.filter_listings(cards, ZIPS, 2_000_000) == []


def test_classify_subjects():
    assert redfin_email.classify("2 homes like X just sold.") == "sold"
    assert redfin_email.classify("Price drop: 1 Elm St") == "price_change"
    assert redfin_email.classify("Price increase to $1.68M on 1 Elm St") == "price_change"
    assert redfin_email.classify("Back on market: 1 Elm St") == "back_on_market"
    assert redfin_email.classify("3 new homes match your search") == "for_sale"


def test_parse_and_filter_synthetic_alert():
    html_ = (_card(1, 1_500_000, "1 Elm St", "Oakland, CA 94607")
             + _card(2, 1_200_000, "2 Oak Ave", "San Bruno, CA 94066")          # outside zips
             + _card(3, 2_100_000, "3 Pine Rd", "Berkeley, CA 94704")            # over budget
             + _card(4, 1_800_000, "4 Fir Ct Unit 2", "Berkeley, CA 94704")      # unit -> not SFH
             + _card(5, 1_750_000, "5 Ash Way", "Berkeley, CA 94704", baths=2.5))
    cards = redfin_email.parse_email("New homes for you", html_)
    kept = realestate.filter_listings(cards, ZIPS, 2_000_000)
    assert [l.address for l in kept] == ["5 Ash Way", "1 Elm St"]  # sorted by city, price
    assert kept[0].baths == 2.5 and kept[1].price_per_sqft == 1000


def test_safelinks_unwrapped():
    wrapped = ('https://nam12.safelinks.protection.outlook.com/?url='
               'https%3A%2F%2Fredmail3.redfin.com%2Fa%2Fclick%3F_e%3Dabc&amp;data=1')
    html_ = _card(9, 1_000_000, "9 Bay Rd", "Berkeley, CA 94704").replace(
        "https://redmail3.redfin.com/a/click?_e=addr9", wrapped)
    assert redfin_email.parse_email("New", html_)[0].url == "https://redmail3.redfin.com/a/click?_e=abc"


def test_llm_fallback_validates_output():
    reply = '```json\n[{"address":"7 Elm St","city":"Berkeley","zip":"94704","price":1500000,"beds":3,' \
            '"baths":2,"sqft":1400},{"address":"","zip":"x","price":"?"}]\n```'
    cards = redfin_email.parse_email("2 new homes for you", "<div>$1,500,000 7 Elm St</div>", llm=lambda p: reply)
    assert len(cards) == 1 and cards[0].key == "addr:7 elm st|94704" and cards[0].baths == 2.0
    assert redfin_email.parse_email("x", "<div>$1</div>", llm=lambda p: "not json") == []


def test_diff_new_price_change_and_sold():
    db = DB(":memory:")
    l = redfin_email.parse_email("New", _card(1, 1_500_000, "1 Elm St", "Oakland, CA 94607"))
    assert len(realestate.diff_and_store(db, l).new) == 1
    assert realestate.diff_and_store(db, l).new == []  # same listing again -> nothing new
    cheaper = redfin_email.parse_email("Price drop on 1 Elm St",
                                       _card(1, 1_450_000, "1 Elm St", "Oakland, CA 94607", was=1_500_000))
    d = realestate.diff_and_store(db, cheaper)
    assert d.price_changes[0][1] == 1_500_000
    ((_, cap),) = realestate.alert_items(d)
    assert "💲 Price drop" in cap and "🔻 −$50,000 (was $1,500,000)" in cap
    sold = redfin_email.parse_email("1 home just sold", _card(1, 1_480_000, "1 Elm St", "Oakland, CA 94607"))
    assert len(realestate.diff_and_store(db, sold).sold) == 1
    assert realestate.diff_and_store(db, sold).sold == []  # reported once


def test_processed_emails_and_kv():
    db = DB(":memory:")
    db.mark_emails_processed(["<a@x>", "<b@x>"])
    db.mark_emails_processed(["<a@x>"])
    assert db.processed_email_ids() == {"<a@x>", "<b@x>"}
    db.set_kv("rotating:discover_it:2026Q4", '["Amazon.com"]')
    assert cards.load_overrides(db, date(2026, 11, 1)) == {"discover_it": ["Amazon.com"]}
    assert cards.load_overrides(db, date(2027, 1, 1)) == {}


def test_fred_parse():
    csv = "observation_date,MORTGAGE30US\n" + "\n".join(
        f"2025-{(i // 4) + 1:02d}-{(i % 4) * 7 + 1:02d},{6 + i / 100:.2f}" for i in range(60))
    p = mortgage.parse_fred_csv("MORTGAGE30US", csv)
    assert p.rate == pytest.approx(6.59) and p.prev_rate == pytest.approx(6.58)
    assert p.year_ago_rate == pytest.approx(6.07)
    assert "30-yr fixed: <b>6.59%</b>" in mortgage.format_rates([p])


CARDS = {
    "discover_it": {"name": "Discover it", "owner": "user_a", "unit": "%", "rates": {"other": 1},
                    "rotating": {"rate": 5, "cap_per_quarter": 1500, "requires_activation": True,
                                 "calendar_url": "https://x", "quarters": {"2026Q4": ["Amazon.com", "Groceries"]}}},
}


def test_quarter_key():
    assert cards.quarter_key(date(2026, 10, 1)) == "2026Q4"
    assert cards.quarter_key(date(2027, 3, 31)) == "2027Q1"


def test_quarterly_message_filled_and_missing():
    msg = cards.quarterly_message(CARDS, "user_a", "Husband", date(2026, 10, 1))
    assert "5% (up to $1,500 spend): Amazon.com, Groceries" in msg and "Activate" in msg
    missing = cards.quarterly_message(CARDS, "user_a", "Husband", date(2027, 1, 1))
    assert "not set" in missing
    assert cards.quarterly_message(CARDS, "user_b", "Wife", date(2026, 10, 1)) is None


def test_best_card():
    assert cards.best_card(CARDS, "user_a", "groceries", date(2026, 11, 1))[0][0] == 5
    assert cards.best_card(CARDS, "user_a", "gas", date(2026, 11, 1))[0][0] == 1
    # /setcats override wins over cards.yaml
    assert cards.best_card(CARDS, "user_a", "gas", date(2026, 11, 1), {"discover_it": ["Gas"]})[0][0] == 5
    msg = cards.quarterly_message(CARDS, "user_a", "H", date(2027, 1, 1), {"discover_it": ["Gas", "EV charging"]})
    assert "Gas, EV charging" in msg and "not set" not in msg


def test_offer_parse_and_db():
    mine = cards.cards_for(CARDS, "user_a")
    o = cards.parse_offer_args("discover_it | Dell | 10% back | 2026-11-30", mine)
    assert o.expires == date(2026, 11, 30)
    with pytest.raises(cards.OfferParseError):
        cards.parse_offer_args("amex_gold | Dell | 10%", mine)
    with pytest.raises(cards.OfferParseError):
        cards.parse_offer_args("discover_it | Dell", mine)
    db = DB(":memory:")
    oid = db.add_offer("user_a", o.card_id, o.merchant, o.description, o.expires)
    rows = db.active_offers("user_a", date(2026, 11, 23))
    assert cards.expiring_offers(rows, date(2026, 11, 23), [7, 1])[0][1] == 7
    assert db.active_offers("user_b") == []
    assert not db.deactivate_offer(oid, "user_b") and db.deactivate_offer(oid, "user_a")


def test_user_routing():
    db = DB(":memory:")
    db.register_user(111, "user_a", "A")
    db.register_user(222, "user_b", "B")
    db.register_user(111, "user_a", "A2")  # re-register is idempotent
    assert sorted(db.chat_ids_for(["user_a", "user_b"])) == [111, 222]
    assert db.chat_ids_for(["user_b"]) == [222]


def test_split_message():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = split_message(text, 1000)
    assert all(len(c) <= 1000 for c in chunks) and "\n".join(chunks) == text


def test_placeholder_renders_png():
    png = placeholder.render(date(2026, 9, 1), "Monthly parking", "$100.00")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000
    assert placeholder.period_for(date(2026, 2, 1)) == (date(2026, 2, 1), date(2026, 2, 28))


def test_account_emails_never_reach_llm():
    calls = []
    llm = lambda p: calls.append(p) or "[]"
    body = "<div>$5 credit. Your code is 107227</div>"
    for subj in ["Your one-time passcode is 107227", "Verify your account", "Reset your password"]:
        assert redfin_email.parse_email(subj, body, llm=llm) == []
    assert redfin_email.parse_email("Redfin newsletter: market trends", body, llm=llm) == []  # no listing hint
    assert calls == []
    redfin_email.parse_email("3 new homes match your search", body, llm=llm)
    assert len(calls) == 1


def test_deep_merge_layers():
    from app.config import deep_merge
    base = {"a": {"x": 1, "y": 2}, "zips": {"94103": "SF"}, "lst": [1, 2], "gone": 1}
    local = {"a": {"y": 3}, "zips": {"_replace": True, "94110": "San Francisco"}, "lst": [9], "gone": None}
    assert deep_merge(base, local) == {"a": {"x": 1, "y": 3}, "zips": {"94110": "San Francisco"}, "lst": [9]}


# ---- morning brief ----------------------------------------------------------------

def _facts():
    from datetime import date as d
    f1 = brief.TickerFacts("ACME", "Acme Rocket Works", "EQUITY", 152.16, -2.8, 4.1, 2.4, -1.0,
                           d(2026, 11, 3))
    f1.headlines = [brief.Headline("ACME-1", "Acme stock is down on CoreWeave troubles", "Barron's", "https://b/1", 1.7)]
    f2 = brief.TickerFacts("IDXF", "Index Fund ETF", "ETF", 707.4, -1.0)
    f3 = brief.TickerFacts("FRUT", "Fruit Computer Inc.", "EQUITY", 343.0, 0.5, earnings=d(2026, 12, 30))
    return {"ACME": f1, "IDXF": f2, "FRUT": f3}


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
    assert "&lt;AI debt&gt;" in out and '<a href="https://b/1">Barron&#x27;s</a>' in out
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


# ---- feature access ------------------------------------------------------------------

def test_access_allowed_paused_recipients():
    from app.access import Access
    db = DB(":memory:")
    a = Access({"me": {"features": "all"}, "partner": {"features": ["realestate"]}, "x": {}}, db)
    assert a.allowed("partner") == ["realestate"] and "market" in a.allowed("me") and "cards" in a.allowed("x")
    assert a.recipients("realestate") == ["me", "partner", "x"] and a.recipients("market") == ["me", "x"]
    a.set_paused("me", "market", True)
    assert a.recipients("market") == ["x"] and not a.receives("me", "market") and a.receives("me", "cards")
    a.set_paused("me", "market", False)
    assert a.receives("me", "market")
    assert "⏸️" not in a.describe("me") and "market" not in a.describe("partner")


# ---- LLM circuit breaker ---------------------------------------------------------------

class _FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def _fake_client(behaviour, calls):
    """behaviour: model -> 'ok' | int error code."""
    from types import SimpleNamespace
    from google.genai import errors

    def generate_content(model, contents, config=None):
        calls.append(model)
        b = behaviour[model]
        if b == "ok":
            return SimpleNamespace(text=f"answer from {model}")
        raise errors.APIError(b, {"error": {"code": b, "message": "high demand", "status": "UNAVAILABLE"}})
    return lambda: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))


def test_breaker_skips_failing_model_then_reprobes():
    from app.modules import llm
    clock, db = _FakeClock(), DB(":memory:")
    health = llm.ModelHealth(db.get_kv, db.set_kv, clock=clock)
    behaviour, calls = {"strong": 503, "lite": "ok"}, []
    run = lambda: llm.summarize("k", ["strong", "lite"], "p", attempts_per_model=3, health=health,  # noqa: E731
                                _client_factory=_fake_client(behaviour, calls), _sleep=lambda s: None)
    assert run() == "answer from lite" and calls == ["strong", "strong", "strong", "lite"]
    assert health.cooling("strong") and health.order(["strong", "lite"]) == ["lite", "strong"]

    calls.clear()                         # next call: straight to the working model, no wasted retries
    assert run() == "answer from lite" and calls == ["lite"]

    # persisted: a fresh ModelHealth (e.g. after a restart) still knows
    assert llm.ModelHealth(db.get_kv, db.set_kv, clock=clock).cooling("strong")

    clock.t += llm.COOLDOWN_503_BASE + 1  # cooldown over: strong gets probed again and wins when recovered
    behaviour["strong"], calls[:] = "ok", []
    assert run() == "answer from strong" and calls == ["strong"] and not health.cooling("strong")


def test_breaker_backoff_grows_and_404_429():
    from app.modules import llm
    clock = _FakeClock()
    h = llm.ModelHealth(clock=clock)
    h.record_fail("m", 503)
    first = h.state["m"]["until"] - clock.t
    h.record_fail("m", 503)
    assert h.state["m"]["until"] - clock.t == 2 * first
    for _ in range(10):
        h.record_fail("m", 503)
    assert h.state["m"]["until"] - clock.t == llm.COOLDOWN_MAX
    h.record_fail("gone", 404)
    h.record_fail("busy", 429)
    assert h.state["gone"]["until"] - clock.t == llm.COOLDOWN_404 and h.state["busy"]["until"] - clock.t == llm.COOLDOWN_429


def test_all_models_cooling_still_tries_each_once():
    from app.modules import llm
    clock = _FakeClock()
    h = llm.ModelHealth(clock=clock)
    h.record_fail("a", 503)
    h.record_fail("b", 503)
    calls = []
    out = llm.summarize("k", ["a", "b"], "p", attempts_per_model=3, health=h,
                        _client_factory=_fake_client({"a": 503, "b": "ok"}, calls), _sleep=lambda s: None)
    assert out == "answer from b" and calls == ["a", "b"]  # last resort, one probe each, no retry storm


def test_parse_saved_search_alert_template():
    html_ = (FIXTURES / "redfin_saved_search.html").read_text()
    assert redfin_email.parse_search_meta(html_) == "example search · <$1.2M, House"
    alert, similar = redfin_email.parse_email("Price increase to $1.15M on 10 Sample St", html_)
    assert (alert.key, alert.event, alert.kind, alert.section) == ("redfin:900000001", "Price Increase", "price_change", "alert")
    assert (alert.address, alert.city, alert.zip, alert.price, alert.was_price) == ("10 Sample St", "Oakland", "94607", 1150000, 999000)
    assert (alert.beds, alert.baths, alert.sqft) == (2, 1.0, None)          # "—" sqft; lot size ignored
    assert alert.url == "https://redmail3.redfin.com/a/click?_e=addr1"
    assert (similar.section, similar.sqft, similar.baths, similar.key) == ("similar", 1800, 2.5, "redfin:900000002")


def test_report_header_sections_and_similar_tracking():
    db = DB(":memory:")
    html_ = (FIXTURES / "redfin_saved_search.html").read_text()
    cards = redfin_email.parse_email("Price increase", html_)
    kept = realestate.filter_listings(cards, ZIPS)                          # no price cap by default
    d = realestate.diff_and_store(db, kept)
    assert [l.address for l, _ in d.price_changes] == ["10 Sample St"] and [l.address for l in d.similar] == ["20 Example Ave"]
    ((alert, cap),) = realestate.alert_items(d, "example search · <$1.2M, House")
    assert cap.splitlines()[0] == "<b>💲 Price increase</b>" and "🔺 +$151,000 (was $999,000)" in cap
    assert "<i>example search · &lt;$1.2M, House</i>" in cap and "2 bd · 1 ba" in cap and len(cap) < 1024
    assert alert.photo.startswith("https://www.redfin.com/stingray/do/api-get-listing-hero-shot/")
    text = realestate.summary_text(d, ["example search · <$1.2M, House"])
    assert "“example search · &lt;$1.2M, House”" in text and "≤" not in text
    assert "Similar homes Redfin suggests (1)" in text and "may be outside your search" in text and "10 Sample St" not in text
    d2 = realestate.diff_and_store(db, kept)                                # same email again: nothing new
    assert not d2.similar and not d2.price_changes
    # a home first seen as a suggestion that later shows up as a real alert counts as new
    as_alert = [realestate.Listing(**{**similar.__dict__, "section": "alert", "kind": "for_sale"})
                for similar in kept if similar.section == "similar"]
    assert [l.address for l in realestate.diff_and_store(db, as_alert).new] == ["20 Example Ave"]
    assert realestate.filter_listings(cards, ZIPS, max_price=1_200_000)[0].address == "10 Sample St"


def test_resolve_links():
    assert redfin_email.resolve_link("https://example.com/x") == "https://example.com/x"   # only redmail hosts
    ls = redfin_email.parse_email("New homes", (FIXTURES / "redfin_saved_search.html").read_text())
    out = redfin_email.with_resolved_links(ls, resolve=lambda u: u.replace("redmail3.redfin.com/a/click?_e=", "www.redfin.com/home/"))
    assert out[0].url == "https://www.redfin.com/home/addr1"


def test_llm_fallback_recovers_link_from_anchor():
    html_ = '<a href="https://redmail3.redfin.com/a/click?_e=zz">7 Elm St, Berkeley, CA 94704</a> $1,500,000'
    reply = '[{"address": "7 Elm St", "city": "Berkeley", "zip": "94704", "price": 1500000}]'
    (l,) = redfin_email.parse_with_llm(html_, "for_sale", lambda p: reply)
    assert l.url == "https://redmail3.redfin.com/a/click?_e=zz"


def test_summary_none_when_only_alerts_and_fetch_photo_guards():
    assert realestate.summary_text(realestate.Diff(), []) is None
    assert redfin_email.fetch_photo("") is None and redfin_email.fetch_photo("https://evil.example/x.jpg") is None


def test_error_alerts_throttled():
    import asyncio
    from app.jobs import Jobs
    from types import SimpleNamespace
    sent = []

    class N:
        async def send_text(self, profiles, text):
            sent.append(text)
    cfg = SimpleNamespace(profiles={"me": {"primary": True}}, primary_profile="me")
    j = Jobs.__new__(Jobs)
    j.cfg, j.db, j.n = cfg, DB(":memory:"), N()
    for _ in range(3):
        asyncio.run(j._report_error("realestate", RuntimeError("imap down")))
    asyncio.run(j._report_error("market", RuntimeError("yahoo down")))
    assert len(sent) == 2 and "realestate" in sent[0] and "market" in sent[1]
