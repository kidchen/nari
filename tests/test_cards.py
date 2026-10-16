"""Cards tests."""
from datetime import date

from helpers import (
    CARDS,
    CHASE_EMAIL,
    DISCOVER_EMAIL,
)

from app.db import DB
from app.features.cards import cards


def test_quarter_keys():
    assert cards.quarter_key(date(2026, 10, 1)) == "2026Q4" and cards.quarter_key(date(2027, 3, 31)) == "2027Q1"
    assert cards.next_quarter_key(date(2026, 11, 5)) == "2027Q1" and cards.next_quarter_key(date(2027, 2, 1)) == "2027Q2"


def test_parse_discover_email():
    from app.features.cards import rotating
    r = rotating.parse_email("Discover <discover@card-e.em.discover.com>", "Activation required: Earn 5% cash back",
                             DISCOVER_EMAIL)
    (q,) = r.quarters
    assert (r.issuer, q.quarter, q.categories, q.cap, q.activated, q.preview) == \
        ("discover", "2026Q4", ["Restaurants", "Entertainment", "Utilities"], 1500, False, False)
    assert q.activate_url == "https://card-e.example.discover.com/click?x=1"


def test_parse_forwarded_chase_email_with_preview():
    from app.features.cards import rotating
    r = rotating.parse_email("Me <me@outlook.example>", "Fw: Congrats! Your 5% cash back is activated.", CHASE_EMAIL)
    cur, nxt = r.quarters
    assert r.issuer == "chase" and cur.activated is True and cur.quarter == "2026Q4"
    assert cur.categories == ["Dining", "Grocery Stores (excluding Walmart and Target)", "Donations to the American Red Cross"]
    assert (nxt.quarter, nxt.categories, nxt.preview) == ("2027Q1", ["Grocery stores", "Top streaming services"], True)


def test_not_an_issuer_email_and_redaction():
    from app.features.cards import rotating
    assert rotating.parse_email("x@random.com", "5% off shoes", "<p>5% off</p>").quarters == []
    red = rotating.redact("sent to: someone@example.com for account number ending with 1234. Call 8005551234")
    assert "someone" not in red and "1234" not in red and "8005551234" not in red


def test_merge_keeps_activation_and_prefers_confirmed():
    from app.features.cards.rotating import QuarterInfo, merge
    preview = QuarterInfo("chase", "2027Q1", ["Grocery stores"], preview=True)
    confirmed = QuarterInfo("chase", "2027Q1", ["Grocery stores", "Streaming"], 1500, activated=False)
    m1 = merge(preview, confirmed)
    assert m1.categories == ["Grocery stores", "Streaming"] and not m1.preview
    m2 = merge(m1, QuarterInfo("chase", "2027Q1", ["Grocery stores", "Streaming"], 1500, activated=True))
    m3 = merge(m2, QuarterInfo("chase", "2027Q1", ["Grocery stores", "Streaming"], 1500, activated=False))
    assert m2.activated is True and m3.activated is True          # activation is sticky
    assert merge(m3, preview).categories == ["Grocery stores", "Streaming"]  # a later preview can't downgrade


def test_card_message_sources_priority_and_activation():
    from app.features.cards import rotating
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
    from app.features.cards import rotating
    db = DB(":memory:")
    for q in rotating.parse_email("me@outlook.example", "Fw: 5% activated", CHASE_EMAIL).quarters:
        cards.save_email_quarter(db, q)
    best = cards.best_card(CARDS, "user_a", "dining", date(2026, 11, 1), db)
    assert best[0] == (5.0, "Freedom Card", "2026Q4 5% category")
    assert cards.best_card(CARDS, "user_a", "gas", date(2026, 11, 1), db)[0][0] == 1


def test_manual_activation_for_issuers_without_confirmation_email():
    from app.features.cards import rotating
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


def test_card_status_is_per_person_categories_shared():
    from app.features.cards import rotating
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

    from app.features.cards.feature import email_owner
    from app.services import Services
    cfg = SimpleNamespace(settings={}, cards={"x": {"owner": "a", "rotating": {"issuer": "chase"}}},
                          profiles={"a": {"features": "all", "email_from": ["a@outlook.example"]},
                                    "b": {"features": ["realestate"]}}, primary_profile="a", gemini_api_key="")
    j = Services(cfg, DB(":memory:"), None)
    owners = {"a@outlook.example": "a"}
    assert email_owner(j, "me <a@outlook.example>", owners) == "a"
    assert email_owner(j, "chase@mcmap.chase.com", owners) == "a"   # only one person has cards: unambiguous
    cfg.profiles["b"] = {"features": "all"}
    cfg.cards["y"] = {"owner": "b", "rotating": {"issuer": "chase"}}
    j2 = Services(cfg, DB(":memory:"), None)
    assert email_owner(j2, "chase@mcmap.chase.com", owners) is None  # two holders: don't guess
