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
    db.set_kv("k", "v")
    assert db.get_kv("k") == "v" and db.get_kv("missing") is None


def test_fred_parse():
    csv = "observation_date,MORTGAGE30US\n" + "\n".join(
        f"2025-{(i // 4) + 1:02d}-{(i % 4) * 7 + 1:02d},{6 + i / 100:.2f}" for i in range(60))
    p = mortgage.parse_fred_csv("MORTGAGE30US", csv)
    assert p.rate == pytest.approx(6.59) and p.prev_rate == pytest.approx(6.58)
    assert p.year_ago_rate == pytest.approx(6.07)
    assert "30-yr fixed: <b>6.59%</b>" in mortgage.format_rates([p])


CARDS = {
    "disc": {"name": "Disc Card", "owner": "user_a", "unit": "%", "rates": {"other": 1},
             "rotating": {"issuer": "discover", "rate": 5, "cap_per_quarter": 1500, "requires_activation": True,
                          "confirms_activation": False, "quarters": {"2026Q4": ["Static Category"]}}},
    "frdm": {"name": "Freedom Card", "owner": "user_a", "unit": "%", "rates": {"other": 1, "dining": 3},
             "rotating": {"issuer": "chase", "rate": 5, "requires_activation": True}},
    "flat": {"name": "Flat Card", "owner": "user_b", "rates": {"other": 2}},
}

DISCOVER_EMAIL = """<html><body><p>Get 5% cash back at Restaurants, Entertainment and Utilities Oct. 1-Dec. 31</p>
<a href="https://card-e.example.discover.com/click?x=1"><span>Activate 5%</span></a>
<p>This email was sent to: someone@example.com for Discover card account number ending with 1234.</p>
<p>1. Activate to earn 5% Cashback Bonus at Restaurants, Entertainment and Utilities from 10/1/2026 (or the date on
which you activate 5%, whichever is later) through 12/31/2026, on up to $1,500 in purchases.</p></body></html>"""

CHASE_EMAIL = """<html><body><p>From: Chase Freedom <chase@mcmap.chase.com></p>
<td>Earn 5% cash back on up to $1,500 in combined purchases on Dining, Grocery Stores (excluding Walmart &reg; and
Target &reg; *) and donations to the American Red Cross from 10/1/26 to 12/31/26.*</td>
<td>Get ready for next quarter</td><td>Get ready for next quarter</td><td>grocery stores</td><td>top streaming services</td>
<td>Earn in these categories from 1/1/27 to 3/31/27</td></body></html>"""


def test_quarter_keys():
    assert cards.quarter_key(date(2026, 10, 1)) == "2026Q4" and cards.quarter_key(date(2027, 3, 31)) == "2027Q1"
    assert cards.next_quarter_key(date(2026, 11, 5)) == "2027Q1" and cards.next_quarter_key(date(2027, 2, 1)) == "2027Q2"


def test_parse_discover_email():
    from app.modules import rotating
    r = rotating.parse_email("Discover <discover@card-e.em.discover.com>", "Activation required: Earn 5% cash back",
                             DISCOVER_EMAIL)
    (q,) = r.quarters
    assert (r.issuer, q.quarter, q.categories, q.cap, q.activated, q.preview) == \
        ("discover", "2026Q4", ["Restaurants", "Entertainment", "Utilities"], 1500, False, False)
    assert q.activate_url == "https://card-e.example.discover.com/click?x=1"


def test_parse_forwarded_chase_email_with_preview():
    from app.modules import rotating
    r = rotating.parse_email("Me <me@outlook.example>", "Fw: Congrats! Your 5% cash back is activated.", CHASE_EMAIL)
    cur, nxt = r.quarters
    assert r.issuer == "chase" and cur.activated is True and cur.quarter == "2026Q4"
    assert cur.categories == ["Dining", "Grocery Stores (excluding Walmart and Target)", "Donations to the American Red Cross"]
    assert (nxt.quarter, nxt.categories, nxt.preview) == ("2027Q1", ["Grocery stores", "Top streaming services"], True)


def test_not_an_issuer_email_and_redaction():
    from app.modules import rotating
    assert rotating.parse_email("x@random.com", "5% off shoes", "<p>5% off</p>").quarters == []
    red = rotating.redact("sent to: someone@example.com for account number ending with 1234. Call 8005551234")
    assert "someone" not in red and "1234" not in red and "8005551234" not in red


def test_merge_keeps_activation_and_prefers_confirmed():
    from app.modules.rotating import QuarterInfo, merge
    preview = QuarterInfo("chase", "2027Q1", ["Grocery stores"], preview=True)
    confirmed = QuarterInfo("chase", "2027Q1", ["Grocery stores", "Streaming"], 1500, activated=False)
    m1 = merge(preview, confirmed)
    assert m1.categories == ["Grocery stores", "Streaming"] and not m1.preview
    m2 = merge(m1, QuarterInfo("chase", "2027Q1", ["Grocery stores", "Streaming"], 1500, activated=True))
    m3 = merge(m2, QuarterInfo("chase", "2027Q1", ["Grocery stores", "Streaming"], 1500, activated=False))
    assert m2.activated is True and m3.activated is True          # activation is sticky
    assert merge(m3, preview).categories == ["Grocery stores", "Streaming"]  # a later preview can't downgrade


def test_card_message_sources_priority_and_activation():
    from app.modules import rotating
    db = DB(":memory:")
    q4 = date(2026, 10, 2)
    msg = cards.quarterly_message(CARDS, "user_a", "A", q4, db)
    assert "Static Category" in msg and "not known yet" in msg               # config fallback; chase unknown
    for body, sender, subj in [(DISCOVER_EMAIL, "discover@card-e.em.discover.com", "Activation required: 5%"),
                               (CHASE_EMAIL, "me@outlook.example", "Fw: Your 5% cash back is activated")]:
        for q in rotating.parse_email(sender, subj, body).quarters:
            cards.save_email_quarter(db, q)
            cards.save_owner_status(db, "user_a", q)
    msg = cards.quarterly_message(CARDS, "user_a", "A", q4, db)
    assert "5%: Restaurants, Entertainment, Utilities · up to $1,500" in msg and "Static Category" not in msg
    assert "👉 <b>activate</b>" in msg and "tap ✅ below" in msg and 'href="https://card-e.example.discover.com/click?x=1"' in msg
    assert "✅ activated" in msg and "Next (2027Q1, preview): Grocery stores, Top streaming services" in msg
    assert cards.needs_activation(CARDS, "user_a", q4, db) == ["Disc Card"]
    db.set_kv(cards.override_key("disc", "2026Q4"), '["Gas"]')               # /setcats wins
    assert "5%: Gas" in cards.quarterly_message(CARDS, "user_a", "A", q4, db)
    assert cards.quarterly_message(CARDS, "user_b", "B", q4, db) is None      # no rotating cards


def test_best_card():
    from app.modules import rotating
    db = DB(":memory:")
    for q in rotating.parse_email("me@outlook.example", "Fw: 5% activated", CHASE_EMAIL).quarters:
        cards.save_email_quarter(db, q)
    best = cards.best_card(CARDS, "user_a", "dining", date(2026, 11, 1), db)
    assert best[0] == (5.0, "Freedom Card", "2026Q4 5% category")
    assert cards.best_card(CARDS, "user_a", "gas", date(2026, 11, 1), db)[0][0] == 1


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


# ---- Gmail session: one login, headers first, bodies only for new mail ------------------------

class _FakeIMAP:
    logins = 0

    def __init__(self, host):
        self.box, self.calls = None, []
        self.boxes = {
            "INBOX": {b"1": (b"<old@redfin>", b"Subject: Old\r\nFrom: listings@redfin.com\r\nMessage-ID: <old@redfin>\r\n\r\nhi"),
                      b"2": (b"<new@redfin>", b"Subject: New\r\nFrom: listings@redfin.com\r\nMessage-ID: <new@redfin>\r\n\r\nhey")},
            "[Gmail]/Spam": {b"7": (b"<card@x>", b"Subject: Fw: 5%\r\nFrom: me@outlook.example\r\nMessage-ID: <card@x>\r\n\r\nyo")},
        }

    def login(self, user, pw):
        _FakeIMAP.logins += 1

    def select(self, box, readonly=False):
        assert readonly, "must open mailboxes read-only"
        self.box = box.strip('"')
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        return ("OK", [b" ".join(self.boxes.get(self.box, {}).keys())])

    def fetch(self, nums, what):
        self.calls.append(what)
        assert "PEEK" in what, "must never mark mail as read"
        out = []
        for n in nums.split(b","):
            mid, raw = self.boxes[self.box][n]
            out.append((n + b" (BODY[...] {1}", f"Message-ID: {mid.decode()}\r\n\r\n".encode() if "HEADER" in what else raw))
            out.append(b")")
        return ("OK", out)

    def logout(self):
        pass


def test_gmail_session_headers_first(monkeypatch):
    from app.modules import gmail
    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", _FakeIMAP)
    _FakeIMAP.logins = 0
    with gmail.Session("a", "b") as g:
        redfin = g.search(["FROM", '"redfin.com"'], 14, {"<old@redfin>"}, ("INBOX",))
        card = g.search(["SUBJECT", '"5%"'], 120, {"<old@redfin>"}, ("INBOX", gmail.SPAM))
        bodies = [c for c in g.imap.calls if c == "(BODY.PEEK[])"]
    assert _FakeIMAP.logins == 1                                     # one login for both searches
    assert [m.subject for m in redfin] == ["New"]                     # processed mail skipped ...
    assert [m.message_id for m in card] == ["<new@redfin>", "<card@x>"]
    assert len(bodies) == 3 and g.stats["headers"] == 5               # ... and its body never downloaded


def test_manual_activation_for_issuers_without_confirmation_email():
    from app.modules import rotating
    db = DB(":memory:")
    q4 = date(2026, 10, 2)
    for body, sender, subj in [(DISCOVER_EMAIL, "discover@card-e.em.discover.com", "Activation required: 5%"),
                               (CHASE_EMAIL, "me@outlook.example", "Fw: Your 5% cash back is activated")]:
        for q in rotating.parse_email(sender, subj, body).quarters:
            cards.save_email_quarter(db, q)
            cards.save_owner_status(db, "user_a", q)
    # Discover never confirms by email -> a button; Chase confirmed by email -> no button
    assert cards.activation_buttons(CARDS, "user_a", q4, db) == [("✅ I activated Disc Card", "act:disc:2026Q4")]
    assert cards.needs_activation(CARDS, "user_a", q4, db) == ["Disc Card"]
    cards.mark_activated(db, "disc", "2026Q4")
    assert cards.activation_buttons(CARDS, "user_a", q4, db) == [] and cards.needs_activation(CARDS, "user_a", q4, db) == []
    msg = cards.quarterly_message(CARDS, "user_a", "A", q4, db)
    assert msg.count("✅ activated") == 2 and "tap ✅" not in msg
    # a later "activation required" email for the same quarter doesn't undo the confirmation
    for q in rotating.parse_email("discover@card-e.em.discover.com", "Reminder: activate 5%", DISCOVER_EMAIL).quarters:
        cards.save_email_quarter(db, rotating.merge(cards.load_email_quarter(db, q.issuer, q.quarter), q))
        cards.save_owner_status(db, "user_a", q)
    assert cards.needs_activation(CARDS, "user_a", q4, db) == []
    # next quarter starts unconfirmed again
    assert cards.mark_activated is not None and not db.get_kv(cards.activated_key("disc", "2027Q1"))


def test_buttons_helper():
    from app.notify import buttons
    assert buttons([]) is None
    kb = buttons([("A", "act:a:2026Q4"), ("B", "act:b:2026Q4")])
    assert [[b.callback_data for b in row] for row in kb.inline_keyboard] == [["act:a:2026Q4"], ["act:b:2026Q4"]]


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
    from datetime import datetime as dt, timedelta
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


# ---- retention / backups -------------------------------------------------------------------

def test_prune_retention_rules(tmp_path):
    import json, time as _t
    from app import maintenance as mt
    db = DB(":memory:")
    old, new = "2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"
    with db.tx() as c:
        c.execute("INSERT INTO processed_emails VALUES ('<old>', ?), ('<new>', ?)", (old, new))
        c.execute("INSERT INTO listings VALUES ('a','u','x',1,'for_sale',?,?), ('b','u','y',1,'for_sale',?,?)", (old, old, new, new))
    for k in ["rotq:chase:2026Q2", "rotq:chase:2026Q3", "rotq:chase:2026Q4", "rotq:chase:2027Q1",
              "activated:disc:2025Q4", "rotating:disc:2026Q3"]:
        db.set_kv(k, "{}")
    now = _t.time()
    db.set_kv("err_alert:old", str(now - 40 * 86400)); db.set_kv("err_alert:new", str(now))
    db.set_kv("tick:OLD", json.dumps({"name": "o", "type": "EQUITY", "ts": now - 40 * 86400}))
    db.set_kv("tick:NEW", json.dumps({"name": "n", "type": "EQUITY", "ts": now}))
    db.set_kv("llm_health", json.dumps({"gone": {"last_ok": now - 40 * 86400}, "live": {"last_ok": now}}))
    out = mt.prune(db, today=date(2026, 11, 1), now=now)
    assert out == {"emails": 1, "listings": 1, "re_events": 0, "deliveries": 0, "quarters": 2, "err_alerts": 1,
                   "ticker_info": 1, "llm_models": 1}
    keys = {k for k, _ in db.kv_like("%")}
    assert {"rotq:chase:2026Q3", "rotq:chase:2026Q4", "rotq:chase:2027Q1", "rotating:disc:2026Q3"} <= keys  # prev/cur/next
    assert "rotq:chase:2026Q2" not in keys and "activated:disc:2025Q4" not in keys
    assert json.loads(db.get_kv("llm_health")) == {"live": {"last_ok": now}}


def test_one_time_cleanup_backup_and_retention_guard(tmp_path):
    from app import maintenance as mt
    db = DB(tmp_path / "n.db")
    with db.tx() as c:
        c.execute("CREATE TABLE offers (id INTEGER)")
        c.execute("INSERT INTO listings VALUES ('addr:1 elm|94000','https://www.redfin.com/zipcode/94000','1 Elm',1,'for_sale','t','t')")
        c.execute("INSERT INTO listings VALUES ('redfin:9','https://www.redfin.com/home/9','2 Oak',1,'for_sale','t','t')")
    assert mt.one_time_cleanup(db) == {"legacy_listings": 1, "offers_table": 1}
    assert mt.one_time_cleanup(db) == {"legacy_listings": 0, "offers_table": 0}          # idempotent
    for _ in range(3):
        p = mt.backup(db, tmp_path / "bk", keep=2)
        _t = __import__("time"); _t.sleep(1.01)
    files = sorted((tmp_path / "bk").glob("nari-*.db"))
    assert len(files) == 2 and DB(files[-1]).get_listing("redfin:9") is not None
    ok = {"real_estate": {"email": {"lookback_days": 14}}, "cards": {"email": {"lookback_days": 120}}}
    mt.check_email_retention(ok)
    with pytest.raises(ValueError):
        mt.check_email_retention({**ok, "cards": {"email": {"lookback_days": 200}}})


def test_ticker_info_cache_persists():
    from app.maintenance import TickerInfoCache
    db = DB(":memory:")
    c = TickerInfoCache(db)
    c["IBM"] = ("International Business Machines", "EQUITY")
    assert TickerInfoCache(db)["IBM"] == ("International Business Machines", "EQUITY")



# ---- real estate: per-person delivery ---------------------------------------------------------

class _RecNotifier:
    def __init__(self, db, fail_for=()):
        self.db, self.fail_for, self.sent = db, set(fail_for), []

    async def send_text(self, profiles, text, reply_markup=None):
        for p in profiles:
            if p in self.fail_for:
                raise RuntimeError(f"telegram down for {p}")
            if self.db.chat_ids_for([p]):
                self.sent.append((p, text.splitlines()[0]))
        return 1

    async def send_photo(self, profiles, data, caption):
        return await self.send_text(profiles, caption)


def _re_jobs(db, notifier):
    from types import SimpleNamespace
    from app.jobs import Jobs
    settings = {"real_estate": {"zips": ZIPS, "photos": False, "catch_up_days": 3, "enabled": True,
                                "email": {"llm_fallback": False}}}
    cfg = SimpleNamespace(settings=settings, cards={}, gemini_api_key="", primary_profile="a",
                          profiles={"a": {"features": "all", "primary": True}, "b": {"features": ["realestate"]}})
    return Jobs(cfg, db, notifier)


def test_realestate_delivered_per_person_with_catch_up_and_isolated_failures():
    import asyncio
    from app.modules.gmail import Mail
    db = DB(":memory:")
    db.register_user(1, "a", "A")                                   # b not registered yet
    n = _RecNotifier(db)
    j = _re_jobs(db, n)
    mail = Mail("<m1>", "Price increase on 10 Sample St", "listings@redfin.com",
                (FIXTURES / "redfin_saved_search.html").read_text())
    asyncio.run(j.real_estate(mails=[mail]))
    assert [p for p, _ in n.sent] == ["a", "a"]                     # alert photo + similar-homes summary, to a only
    assert db.processed_email_ids() == {"<m1>"}

    n.sent.clear()
    asyncio.run(j.real_estate(mails=[]))                            # nothing new, nothing resent
    assert n.sent == []

    db.register_user(2, "b", "B")                                   # b registers later ...
    asyncio.run(j.real_estate(mails=[]))
    assert n.sent == []                                             # ... events predate b's registration: no backfill

    db._conn.execute("UPDATE users SET registered='2000-01-01T00:00:00+00:00' WHERE profile='b'")  # b registered long ago
    n.fail_for = {"b"}
    asyncio.run(j.real_estate(mails=[]))                            # b's chat fails: a gets only the error alert,
    assert [t for _, t in n.sent] == ["⚠️ Job <b>realestate delivery</b> failed: b: telegram down for b"]  # no resends
    n.sent.clear()
    n.fail_for = set()
    asyncio.run(j.real_estate(mails=[]))                            # retried next check, b only
    assert [p for p, _ in n.sent] == ["b", "b"]
    n.sent.clear()
    asyncio.run(j.real_estate(mails=[]))
    assert n.sent == []                                             # delivered exactly once each


def test_realestate_paused_person_skipped_then_catches_up():
    import asyncio
    from app.modules.gmail import Mail
    db = DB(":memory:")
    db.register_user(1, "a", "A")
    db.register_user(2, "b", "B")
    db._conn.execute("UPDATE users SET registered='2000-01-01T00:00:00+00:00'")
    n = _RecNotifier(db)
    j = _re_jobs(db, n)
    j.access.set_paused("b", "realestate", True)
    asyncio.run(j.real_estate(mails=[Mail("<m1>", "Price increase", "listings@redfin.com",
                                          (FIXTURES / "redfin_saved_search.html").read_text())]))
    assert {p for p, _ in n.sent} == {"a"}
    n.sent.clear()
    j.access.set_paused("b", "realestate", False)                   # resume: last-3-days catch-up
    asyncio.run(j.real_estate(mails=[]))
    assert [p for p, _ in n.sent] == ["b", "b"]



def test_card_status_is_per_person_categories_shared():
    from app.modules import rotating
    two = {"a_frdm": {"name": "A Freedom", "owner": "user_a", "rates": {"other": 1},
                      "rotating": {"issuer": "chase", "rate": 5, "requires_activation": True}},
           "b_frdm": {"name": "B Freedom", "owner": "user_b", "rates": {"other": 1},
                      "rotating": {"issuer": "chase", "rate": 5, "requires_activation": True}}}
    db = DB(":memory:")
    for q in rotating.parse_email("me@outlook.example", "Fw: Your 5% cash back is activated", CHASE_EMAIL).quarters:
        cards.save_email_quarter(db, q)
        cards.save_owner_status(db, "user_a", q)            # it was A's email
    a = cards.quarterly_message(two, "user_a", "A", date(2026, 10, 2), db)
    b = cards.quarterly_message(two, "user_b", "B", date(2026, 10, 2), db)
    assert "Dining" in a and "Dining" in b                   # categories are public: shared
    assert "✅ activated" in a and "✅ activated" not in b      # activation is A's alone
    assert "rotact:user_a:chase:2026Q4" in {k for k, _ in db.kv_like("rotact:%")}
    assert '"activated"' not in db.get_kv("rotq:chase:2026Q4").replace('"activated": null', "")


def test_card_email_owner_attribution():
    from types import SimpleNamespace
    from app.jobs import Jobs
    cfg = SimpleNamespace(settings={}, cards={"x": {"owner": "a", "rotating": {"issuer": "chase"}}},
                          profiles={"a": {"features": "all", "email_from": ["a@outlook.example"]},
                                    "b": {"features": ["realestate"]}}, primary_profile="a", gemini_api_key="")
    j = Jobs(cfg, DB(":memory:"), None)
    owners = {"a@outlook.example": "a"}
    assert j._email_owner("me <a@outlook.example>", owners) == "a"
    assert j._email_owner("chase@mcmap.chase.com", owners) == "a"   # only one person has cards: unambiguous
    cfg.profiles["b"] = {"features": "all"}
    cfg.cards["y"] = {"owner": "b", "rotating": {"issuer": "chase"}}
    j2 = Jobs(cfg, DB(":memory:"), None)
    assert j2._email_owner("chase@mcmap.chase.com", owners) is None  # two holders: don't guess


def test_realestate_per_person_criteria():
    assert realestate.matches_person(None, "94607", "Oakland", 2_000_000, "x")
    prefs = {"zips": ["94607", "Berkeley"], "max_price": 1_200_000, "searches": ["example"]}
    assert realestate.matches_person(prefs, "94607", "Oakland", 1_150_000, "example search · <$1.2M, House")
    assert realestate.matches_person(prefs, "94704", "Berkeley", 1_000_000, "example search")   # by city name
    assert not realestate.matches_person(prefs, "94103", "San Francisco", 1_000_000, "example search")
    assert not realestate.matches_person(prefs, "94607", "Oakland", 1_300_000, "example search")
    assert not realestate.matches_person(prefs, "94607", "Oakland", 1_000_000, "other search")


def test_realestate_delivery_respects_each_persons_criteria():
    import asyncio
    from app.modules.gmail import Mail
    db = DB(":memory:")
    db.register_user(1, "a", "A")
    db.register_user(2, "b", "B")
    n = _RecNotifier(db)
    j = _re_jobs(db, n)
    j.cfg.profiles["b"]["real_estate"] = {"zips": ["94704"]}          # b only wants Berkeley
    asyncio.run(j.real_estate(mails=[Mail("<m1>", "Price increase", "listings@redfin.com",
                                          (FIXTURES / "redfin_saved_search.html").read_text())]))
    a_msgs = [t for p, t in n.sent if p == "a"]
    b_msgs = [t for p, t in n.sent if p == "b"]
    assert a_msgs == ["<b>💲 Price increase</b>", "🏡 <b>Redfin</b> — “example search · &lt;$1.2M, House”"]
    assert b_msgs == ["🏡 <b>Redfin</b> — “example search · &lt;$1.2M, House”"]   # Oakland alert filtered out for b
    n.sent.clear()
    asyncio.run(j.real_estate(mails=[]))
    assert n.sent == []                                                  # filtered events don't linger



def test_admin_only_features():
    from app.access import Access
    db = DB(":memory:")
    a = Access({"admin": {"primary": True, "features": "all"}, "other": {"features": "all"},
                "listed": {"features": ["realestate", "placeholder"]}}, db)
    assert "placeholder" in a.allowed("admin")
    assert "placeholder" not in a.allowed("other") and a.allowed("listed") == ["realestate"]
    assert a.recipients("placeholder") == ["admin"]


# ---- VM health ------------------------------------------------------------------------------

def _thresholds():
    return {"disk_pct": 85, "mem_pct": 90, "load_per_core": 2.0, "swap_pct": 50, "restarts_24h": 3,
            "backup_max_age_days": 9, "job_failures": 3, "job_failures_hours": 6}


def test_health_alarms_fire_once_and_recover():
    from app.modules import vmhealth as vh
    db = DB(":memory:")
    now = 1_800_000_000.0
    m = vh.Metrics(load15=0.3, cores=1, mem_total_mb=6000, mem_avail_mb=4000, disk_used_pct=40.0, newest_backup_age_d=1)
    assert vh.transitions(db, vh.conditions(m, _thresholds(), now)) == []          # healthy: silent
    m.disk_used_pct = 91.0
    (key, firing, desc), = vh.transitions(db, vh.conditions(m, _thresholds(), now))
    assert (key, firing) == ("disk", True) and "Disk 91% used" in desc
    assert vh.transitions(db, vh.conditions(m, _thresholds(), now)) == []          # still firing: no repeat
    m.disk_used_pct = 50.0
    assert vh.transitions(db, vh.conditions(m, _thresholds(), now)) == [("disk", False, "Disk 50% used (alarm ≥ 85%)")]


def test_health_job_failure_alarm_and_format():
    from app.modules import vmhealth as vh
    db = DB(":memory:")
    now = 1_800_000_000.0
    for i in range(3):
        vh.record_failure(db, "gmail", "IMAP login failed", now - 600 * i)
    vh.record_failure(db, "market", "yahoo 500", now - 20 * 3600)               # old: outside the 6h window
    m = vh.Metrics(cores=1, mem_total_mb=6000, mem_avail_mb=4000, disk_total_gb=30, disk_used_pct=40.0)
    m.job_failures = {"gmail": [now - 600 * i for i in range(3)], "market": [now - 20 * 3600]}
    m.last_errors = {"gmail": "IMAP login failed", "market": "yahoo 500"}
    firing = {k for k, f, _ in vh.conditions(m, _thresholds(), now) if f}
    assert firing == {"job:gmail"}
    m.oci = {"error": "not permitted — create the dynamic group + policy (README → VM health)"}
    text = vh.format_health(m, _thresholds(), now)
    assert "🟢 Disk 40% of 30 GB" in text and "🔴 gmail: 3×" in text and "🟢 market: 1×" in text
    assert "OCI: not permitted" in text


def test_vmhealth_is_admin_only():
    from app.access import Access
    a = Access({"admin": {"primary": True}, "other": {"features": "all"}}, DB(":memory:"))
    assert a.recipients("vmhealth") == ["admin"]


def test_watchdog_alerts_once_and_recovers(tmp_path):
    import os, stat, subprocess
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    status_file, sent_file = tmp_path / "status", tmp_path / "sent"
    (bin_dir / "docker").write_text(f'#!/bin/sh\ncat {status_file}\n')
    (bin_dir / "curl").write_text(f'#!/bin/sh\ncat >/dev/null\nfor a in "$@"; do case "$a" in text=*) echo "$a" >> {sent_file};; esac; done\n')
    (bin_dir / "hostname").write_text("#!/bin/sh\necho vm\n")
    for f in bin_dir.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    env_file, chat = tmp_path / ".env", tmp_path / "admin_chat_id"
    env_file.write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
    chat.write_text("42")
    script = (Path(__file__).parent.parent / "deploy" / "watchdog.sh").read_text()
    script = script.replace("/var/lib/nari-watchdog", str(state)).replace(
        "/var/lib/docker/volumes/nari-data/_data/admin_chat_id", str(chat))
    (tmp_path / "wd.sh").write_text(script)
    run = lambda s: (status_file.write_text(s), subprocess.run(  # noqa: E731
        ["bash", str(tmp_path / "wd.sh"), str(env_file)], env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=True))
    sent = lambda: sent_file.read_text().splitlines() if sent_file.exists() else []  # noqa: E731
    run("running/healthy"); assert sent() == []
    run("exited/none"); assert sent() == []                         # 1st bad check: no page (deploy restarts)
    run("running/starting"); assert sent() == []                    # starting: ignored
    run("exited/none"); assert len(sent()) == 1 and "DOWN" in sent()[0]
    run("exited/none"); assert len(sent()) == 1                     # still down: no repeat
    run("running/healthy"); assert len(sent()) == 2 and "back" in sent()[1]


def test_counter_delta_handles_reset():
    from app.modules.vmhealth import counter_delta
    assert counter_delta([100, 150, 400]) == 300
    assert counter_delta([100, 150, 20, 70]) == 50 + 20 + 50       # reboot reset the counter
    assert counter_delta([1000, 1100, 1090, 1200]) == 100 + 110     # small aggregation dip: not a reset
    assert counter_delta([5]) == 0


def test_restarts_count_crashes_not_deploys():
    from app.modules import vmhealth as vh
    db = DB(":memory:")
    now = 1_800_000_000.0
    for i, cid in enumerate(["c1", "c2", "c3"]):                    # three deploys: new container each time
        vh.record_start(db, now - 3600 * (3 - i), cid)
    assert vh.restarts_24h(db, now) == 0
    vh.record_start(db, now - 60, "c3")                             # Docker restarted the same container
    vh.record_start(db, now - 30, "c3")
    assert vh.restarts_24h(db, now) == 2
    db.set_kv("health:starts", "[1799999000.0, 1799999500.0]")     # old format (plain timestamps) is ignored
    assert vh.restarts_24h(db, now) == 0
