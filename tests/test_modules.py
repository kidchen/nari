from datetime import date
from pathlib import Path

import pytest

from app.db import DB
from app.modules import cards, market, mortgage, placeholder, realestate, redfin_email
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
    assert redfin_email.classify("Price drop: 1 Elm St") == "price_drop"
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
    assert "🔻 was $1,500,000" in realestate.format_report(d, 2_000_000, 1, [])
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


def test_market_format_and_prompt():
    q = market.Quote("NVDA", "NVIDIA Corp", 120.5, 5.2, 9_000_000, 2_000_000)
    assert q.volume_ratio == 4.5
    out = market.format_digest(date(2026, 10, 13), [q], [q], [], [q], "NVDA: <earnings>", [])
    assert "vol 4.5× avg" in out and "&lt;earnings&gt;" in out and "not financial advice" in out
    prompt = market.build_catalyst_prompt([q], {"NVDA": ["NVIDIA beats"]}, ["Stocks rally"])
    assert "NVIDIA beats" in prompt and "Never give buy/sell" in prompt


def test_placeholder_renders_png():
    png = placeholder.render(date(2026, 9, 1), "Monthly parking", "$100.00")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000
    assert placeholder.period_for(date(2026, 2, 1)) == (date(2026, 2, 1), date(2026, 2, 28))


def test_fresh_titles_drops_stale_and_undated():
    import time as _t
    from types import SimpleNamespace as NS
    now = 1_800_000_000
    entries = [NS(title="new", published_parsed=_t.gmtime(now - 3600)),
               NS(title="old", published_parsed=_t.gmtime(now - 40 * 3600)),
               NS(title="undated", published_parsed=None)]
    assert market.fresh_titles(entries, 30, now=now) == ["new"]


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
