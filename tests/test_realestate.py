"""Realestate tests."""

import pytest
from helpers import FIXTURES, ZIPS, _card, _re_jobs, _RecNotifier

from app.db import DB
from app.features.realestate import listings as realestate
from app.features.realestate import mortgage, redfin_email
from app.features.realestate.feature import handle_mail


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


def test_fred_parse():
    csv = "observation_date,MORTGAGE30US\n" + "\n".join(
        f"2025-{(i // 4) + 1:02d}-{(i % 4) * 7 + 1:02d},{6 + i / 100:.2f}" for i in range(60))
    p = mortgage.parse_fred_csv("MORTGAGE30US", csv)
    assert p.rate == pytest.approx(6.59) and p.prev_rate == pytest.approx(6.58)
    assert p.year_ago_rate == pytest.approx(6.07)
    assert "30-yr fixed: <b>6.59%</b>" in mortgage.format_rates([p])


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


def test_realestate_delivered_per_person_with_catch_up_and_isolated_failures():
    import asyncio

    from app.integrations.gmail import Mail
    db = DB(":memory:")
    db.register_user(1, "a", "A")                                   # b not registered yet
    n = _RecNotifier(db)
    j = _re_jobs(db, n)
    mail = Mail("<m1>", "Price increase on 10 Sample St", "listings@redfin.com",
                (FIXTURES / "redfin_saved_search.html").read_text())
    asyncio.run(handle_mail(j, [mail]))
    assert [p for p, _ in n.sent] == ["a", "a"]                     # alert photo + similar-homes summary, to a only
    assert db.processed_email_ids() == {"<m1>"}

    n.sent.clear()
    asyncio.run(handle_mail(j, []))                            # nothing new, nothing resent
    assert n.sent == []

    db.register_user(2, "b", "B")                                   # b registers later ...
    asyncio.run(handle_mail(j, []))
    assert n.sent == []                                             # ... events predate b's registration: no backfill

    db._conn.execute("UPDATE users SET registered='2000-01-01T00:00:00+00:00' WHERE profile='b'")  # b registered long ago
    n.fail_for = {"b"}
    asyncio.run(handle_mail(j, []))                            # b's chat fails: a gets only the error alert,
    assert [t for _, t in n.sent] == ["⚠️ Job <b>realestate delivery</b> failed: b: telegram down for b"]  # no resends
    n.sent.clear()
    n.fail_for = set()
    asyncio.run(handle_mail(j, []))                            # retried next check, b only
    assert [p for p, _ in n.sent] == ["b", "b"]
    n.sent.clear()
    asyncio.run(handle_mail(j, []))
    assert n.sent == []                                             # delivered exactly once each


def test_realestate_paused_person_skipped_then_catches_up():
    import asyncio

    from app.integrations.gmail import Mail
    db = DB(":memory:")
    db.register_user(1, "a", "A")
    db.register_user(2, "b", "B")
    db._conn.execute("UPDATE users SET registered='2000-01-01T00:00:00+00:00'")
    n = _RecNotifier(db)
    j = _re_jobs(db, n)
    j.access.set_paused("b", "realestate", True)
    asyncio.run(handle_mail(j, [Mail("<m1>", "Price increase", "listings@redfin.com",
                                          (FIXTURES / "redfin_saved_search.html").read_text())]))
    assert {p for p, _ in n.sent} == {"a"}
    n.sent.clear()
    j.access.set_paused("b", "realestate", False)                   # resume: last-3-days catch-up
    asyncio.run(handle_mail(j, []))
    assert [p for p, _ in n.sent] == ["b", "b"]


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

    from app.integrations.gmail import Mail
    db = DB(":memory:")
    db.register_user(1, "a", "A")
    db.register_user(2, "b", "B")
    n = _RecNotifier(db)
    j = _re_jobs(db, n)
    j.cfg.profiles["b"]["real_estate"] = {"zips": ["94704"]}          # b only wants Berkeley
    asyncio.run(handle_mail(j, [Mail("<m1>", "Price increase", "listings@redfin.com",
                                          (FIXTURES / "redfin_saved_search.html").read_text())]))
    a_msgs = [t for p, t in n.sent if p == "a"]
    b_msgs = [t for p, t in n.sent if p == "b"]
    assert a_msgs == ["<b>💲 Price increase</b>", "🏡 <b>Redfin</b> — “example search · &lt;$1.2M, House”"]
    assert b_msgs == ["🏡 <b>Redfin</b> — “example search · &lt;$1.2M, House”"]   # Oakland alert filtered out for b
    n.sent.clear()
    asyncio.run(handle_mail(j, []))
    assert n.sent == []                                                  # filtered events don't linger
