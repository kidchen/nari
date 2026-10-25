"""Expenses tests: statement parsing (synthetic layouts modelled on real ones), upload, import, /spend."""
import asyncio
from datetime import date
from types import SimpleNamespace

from helpers import _bot, _call, _Chat, _cmd, _Msg

from app.features.expenses import statements as S


def _words(rows, page=0):
    """rows: list of [(x0, "text"), ...] -> positioned words, one row per line."""
    return [{"text": t, "x0": float(x), "top": 100.0 + 12 * i, "page": page} for i, row in enumerate(rows) for x, t in row]


def _row(x, text, step=30):
    return [(x + step * i, w) for i, w in enumerate(text.split())]


BOA_FIRST = "Bank of America\nAccount# 4000 1234 5678 4321\nSeptember 9 - October 9, 2026\nPrevious Balance $100.00\nNew Balance Total $140.00"
BOA = _words([_row(40, "Transaction Posting Reference Account"),
              _row(40, "Payments and Other Credits"),
              _row(40, "09/20 09/21 ONLINE/MOBILE PAYMENT 1111 4321 -100.00"),
              _row(40, "09/22 09/23 REFUND STORE 2222 4321 -10.00"),
              _row(40, "TOTAL PAYMENTS AND OTHER CREDITS FOR THIS PERIOD -$110.00"),
              _row(40, "Purchases and Adjustments"),
              _row(40, "09/12 09/13 SAFEWAY #1234 SPRINGFIELD CA 3333 4321 45.50"),
              _row(40, "10/01 10/02 SHELL 9999 SHELBYVILLE CA 4444 4321 100.00"),
              _row(40, "Interest Charged"),
              _row(40, "10/09 10/09 INTEREST CHARGED ON PURCHASES 4.50")])

CHASE_FIRST = "chase.com/cardhelp\nNew Balance\nAccount Number: XXXX XXXX XXXX 8765\nOpening/Closing Date 12/10/26 - 01/09/27\nPrevious Balance $50.00\nNew Balance $80.00"
CHASE = _words([_row(40, "PAYMENTS AND OTHER CREDITS"),
                _row(40, "12/28 AUTOMATIC PAYMENT - THANK YOU -50.00"),
                _row(40, "PURCHASE"),
                _row(40, "12/15 NETFLIX.COM ANYTOWN CA 14.99"),
                _row(40, "12/16 PARKING METER .50"),                     # no leading zero, as Chase prints it
                _row(40, "01/03 TRADER JOE S #123 OGDENVILLE CA 55.51"),
                _row(40, "FEES CHARGED"),
                _row(40, "01/09 ANNUAL MEMBERSHIP FEE 9.00"),
                _row(40, "TOTAL FEES FOR THIS PERIOD $9.00")])

DISC_FIRST = "DISCOVER IT CARD ENDING IN 5555\nAccount Summary 09/12/2026 - 10/11/2026\nPrevious Balance $20.00 New Balance Minimum Payment\nNew Balance: $35.00"
DISC = _words([[(38, "DATE"), (80, "PAYMENTS"), (120, "AND"), (136, "CREDITS"), (374, "AMOUNT"), (414, "PREVIOUS"), (560, "$9.99")],
               [(38, "09/15"), (80, "DIRECTPAY"), (124, "FULL"), (144, "BALANCE"), (369, "-$20.00"), (421, "5%"), (566, "+$1.00")],
               [(38, "DATE"), (80, "PURCHASES"), (256, "MERCHANT"), (297, "CATEGORY"), (374, "AMOUNT")],
               [(38, "09/20"), (80, "COSTCO"), (120, "WHSE"), (256, "Warehouse"), (300, "Clubs"), (377, "$35.00")],
               [(414, "5%"), (432, "Cashback"), (481, "Bonus")]])


def test_boa_layout_two_dates_refs_and_sections():
    st = S.parse(BOA, BOA_FIRST)
    assert (st.bank, st.last4, st.start, st.end) == ("boa", "4321", date(2026, 9, 9), date(2026, 10, 9))
    assert [(t.kind, t.description, t.amount_cents) for t in st.txns] == [
        ("payment", "ONLINE/MOBILE PAYMENT", -10000), ("credit", "REFUND STORE", -1000),
        ("purchase", "SAFEWAY #1234 SPRINGFIELD CA", 4550), ("purchase", "SHELL 9999 SHELBYVILLE CA", 10000),
        ("interest", "INTEREST CHARGED ON PURCHASES", 450)]
    assert st.ok and st.balanced


def test_chase_layout_and_year_wrap():
    st = S.parse(CHASE, CHASE_FIRST)
    assert (st.bank, st.last4) == ("chase", "8765") and st.ok
    assert [t.date for t in st.txns] == [date(2026, 12, 28), date(2026, 12, 15), date(2026, 12, 16), date(2027, 1, 3),
                                         date(2027, 1, 9)]
    assert [t.kind for t in st.txns] == ["payment", "purchase", "purchase", "purchase", "fee"]
    assert st.txns[2].amount_cents == 50


def test_discover_crops_rewards_box_and_keeps_category():
    st = S.parse(DISC, DISC_FIRST)
    assert (st.bank, st.last4) == ("discover", "5555") and st.ok
    (pay, buy) = st.txns
    assert (pay.kind, pay.amount_cents) == ("payment", -2000)
    assert (buy.description, buy.bank_category, buy.amount_cents) == ("COSTCO WHSE", "Warehouse Clubs", 3500)


def test_unbalanced_or_unknown_statement_is_refused():
    st = S.parse(BOA[:-1], BOA_FIRST)                     # interest line lost -> doesn't add up
    assert not st.ok and "doesn't add up" in st.problems[-1]
    assert "Unknown bank" in S.parse(BOA, "Some Credit Union").problems[0]
    assert S.detect_bank("purchases and payments") is None   # "chase" inside "purchase" isn't Chase


def _send_file(b, chat_id, name, data, mime="application/pdf"):
    out = []

    async def download():
        return bytearray(data)

    async def get_file():
        return SimpleNamespace(download_as_bytearray=download)
    msg = _Msg("", out)
    msg.document = SimpleNamespace(file_name=name, mime_type=mime, file_size=len(data), get_file=get_file)
    upd = SimpleNamespace(effective_message=msg, effective_chat=_Chat(chat_id, out), effective_user=None, callback_query=None)
    asyncio.run(b.on_document(upd, SimpleNamespace(args=[], user_data={})))
    return out


def _press(b, chat_id, data, out):
    q = SimpleNamespace(data=data, message=SimpleNamespace(text_html="preview"))

    async def answer(*a, **k):
        out.append(("answer", a))

    async def edit(text, **k):
        out.append((text, None))
    q.answer, q.edit_message_text = answer, edit
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), callback_query=q)
    for h in b.app.handlers[0]:
        if getattr(h, "pattern", None) and h.pattern.match(data):
            asyncio.run(h.callback(upd, SimpleNamespace()))
            return


def test_upload_preview_import_and_spend(tmp_path, monkeypatch):
    b, _ = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "parse_pdf", lambda path: S.parse(BOA, BOA_FIRST))
    monkeypatch.setattr(b.services, "today", lambda: date(2026, 10, 9))
    out = _send_file(b, 1, "Statement Sep/2026.pdf", b"%PDF-1.7 fake")
    text, kb = out[-1]
    assert "🧾 <b>Bank of America ··4321</b> · Sep 09 – Oct 09, 2026" in text and "✅ Adds up" in text
    (f,) = (tmp_path / "statements" / "inbox" / "admin").iterdir()
    assert f.name.endswith("-Statement_Sep_2026.pdf") and f.stat().st_mode & 0o777 == 0o600
    token = kb.inline_keyboard[0][0].callback_data
    _press(b, 2, token, out)                                     # someone else's button: refused
    assert ("answer", ("That statement is no longer pending.",)) in out and f.exists()
    _press(b, 1, token, out)
    assert "✅ Imported 5 transactions" in out[-1][0] and not f.exists()      # PDF deleted after import
    _press(b, 1, token, out)                                     # double tap: nothing twice
    assert b.db.rows("SELECT count(*) n FROM expense_txns")[0]["n"] == 5
    out = []
    _call(b, asyncio, _cmd(b, "spend"), 1, "/spend", out)
    t = out[-1][0]
    assert "<b>$140.00</b> (4 items)" in t and "Safeway: $45.50" in t     # payment excluded, refund counted
    assert "• Gas: $100.00" in t and "• Groceries: $45.50" in t and "• Fees &amp; interest: $4.50" in t
    _send_file(b, 1, "again.pdf", b"%PDF-1.7 fake")
    assert "Already imported" in _send_file(b, 1, "again.pdf", b"%PDF-1.7 fake")[-1][0]


def test_upload_rejections(tmp_path, monkeypatch):
    b, _ = _bot(tmp_path, monkeypatch)
    assert "doesn't look like a PDF" in _send_file(b, 1, "x.pdf", b"hello")[-1][0]
    assert _send_file(b, 2, "s.pdf", b"%PDF-1.7")[-1][0] == "I don't know what to do with that file."   # sci: no expenses
    assert _send_file(b, 1, "s.txt", b"text", mime="text/plain")[-1][0] == "I don't know what to do with that file."
    bad = _send_file(b, 1, "broken.pdf", b"%PDF-1.7 not really")[-1][0]           # real parser, unreadable file
    assert "Couldn't import" in bad and "kept on the server for 30 days" in bad
    assert not (tmp_path / "statements" / "inbox" / "sci").exists()


def test_old_statements_are_pruned(tmp_path):
    import os
    import time

    from app import maintenance as mt
    from app.db import DB
    db = DB(tmp_path / "n.db")
    d = tmp_path / "statements" / "inbox" / "admin"
    d.mkdir(parents=True)
    old, new = d / "old.pdf", d / "new.pdf"
    old.write_bytes(b"%PDF"); new.write_bytes(b"%PDF")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    assert mt.prune(db)["statements"] == 1 and not old.exists() and new.exists()


def test_merchant_keys_and_starter_rules():
    from app.features.expenses import categories as C
    assert C.merchant_key("SQ *BLUE DOOR CAFE #12 SPRINGFIELD CA") == "BLUE DOOR"
    assert C.merchant_key("TRADER JOE S #123 OGDENVILLE CA") == "TRADER JOE'S"
    assert C.seed_match("COSTCO GAS #1")[1] == "Gas" and C.seed_match("COSTCO WHSE #9")[1] == "Groceries"
    assert C.seed_match("AMAZON PRIME*AB12")[1] == "Subscriptions" and C.seed_match("AMZN MKTP US*ZZ")[1] == "Shopping"
    assert C.seed_match("TST* LUCKY DRAGON SPRINGFIELD CA")[1] == "Dining"            # Toast = restaurant, not Lucky stores
    assert C.seed_match("UBER *EATS")[1] == "Dining" and C.seed_match("UBER *TRIP")[1] == "Transport"
    assert C.seed_match("STORE #76 TOWN CA") is None                                   # a store number isn't a 76 station


def _db_with(rows):
    from app.db import DB
    db = DB(":memory:")
    with db.tx() as c:
        c.executemany("INSERT INTO expense_txns(id, owner, bank, last4, date, amount, description, kind, bank_category, source) "
                      "VALUES (?,?,?,?,?,?,?,?,?,'statement')", rows)
    return db


def test_categorize_order_learning_and_ai_cache():
    from app.features.expenses import store
    db = _db_with([("1", "a", "discover", "1", "2026-10-01", 1000, "BLUE DOOR BISTRO", "purchase", "Restaurants"),
                   ("2", "a", "chase", "2", "2026-10-02", 2000, "SAFEWAY #1", "purchase", ""),
                   ("3", "a", "chase", "2", "2026-10-03", 3000, "ZORBLAX INDUSTRIES", "purchase", ""),
                   ("4", "a", "chase", "2", "2026-10-04", 500, "INTEREST CHARGED ON PURCHASES", "interest", ""),
                   ("5", "a", "chase", "2", "2026-10-05", -9000, "PAYMENT - THANK YOU", "payment", "")])
    asked = []

    def llm(prompt):
        asked.append(prompt)
        return '{"merchants": {"ZORBLAX INDUSTRIES": "Home", "NOT ASKED": "Dining"}}'
    counts = store.categorize(db, llm)
    assert counts == {"seed": 2, "kind": 1, "ai": 1}         # bistro: seed's generic dining rule beats the bank
    assert "ZORBLAX INDUSTRIES" in asked[0] and "9000" not in asked[0] and "2026" not in asked[0]   # names only
    cats = {r["id"]: r["category"] for r in db.rows("SELECT id, category FROM expense_txns")}
    assert cats == {"1": "Dining", "2": "Groceries", "3": "Home", "4": "Fees & interest", "5": ""}
    with db.tx() as c:                                         # a new charge at the same merchant: no second AI call
        c.execute("INSERT INTO expense_txns(id, owner, bank, last4, date, amount, description, kind, bank_category, source) "
                  "VALUES ('6','a','chase','2','2026-10-09',100,'ZORBLAX INDUSTRIES','purchase','','alert')")
    assert store.categorize(db, llm) == {"ai": 1} and len(asked) == 1


def test_bank_category_is_learned_for_other_cards():
    from app.features.expenses import store
    db = _db_with([("1", "a", "discover", "1", "2026-10-01", 1000, "QUUX MART", "purchase", "Supermarkets"),
                   ("2", "a", "chase", "2", "2026-10-02", 2000, "QUUX MART", "purchase", "")])
    assert store.categorize(db) == {"bank": 2}                  # learned from Discover, reused for the Chase charge
    assert {r["category"] for r in db.rows("SELECT category FROM expense_txns")} == {"Groceries"}


def test_user_fix_beats_everything_and_is_per_person():
    from app.features.expenses import store
    db = _db_with([("1", "a", "chase", "1", "2026-10-01", 1000, "COSTCO WHSE #1", "purchase", ""),
                   ("2", "b", "chase", "2", "2026-10-01", 1000, "COSTCO WHSE #2", "purchase", "")])
    store.categorize(db)
    assert store.set_category(db, "a", "COSTCO", "Shopping") == 1
    with db.tx() as c:
        c.execute("INSERT INTO expense_txns(id, owner, bank, last4, date, amount, description, kind, bank_category, source) "
                  "VALUES ('3','a','chase','1','2026-10-09',100,'COSTCO WHSE #3','purchase','','alert')")
    store.categorize(db)
    cats = {r["id"]: r["category"] for r in db.rows("SELECT id, category FROM expense_txns")}
    assert cats == {"1": "Shopping", "2": "Groceries", "3": "Shopping"}       # b keeps the default


def test_spend_fix_buttons(tmp_path, monkeypatch):
    b, _ = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "parse_pdf", lambda path: S.parse(BOA, BOA_FIRST))
    out = _send_file(b, 1, "s.pdf", b"%PDF-1.7 fake")
    _press(b, 1, out[-1][1].inline_keyboard[0][0].callback_data, out)        # import
    out = []
    _call(b, asyncio, _cmd(b, "spend"), 1, "/spend fix shell", out)
    text, kb = out[-1]
    assert "<b>SHELL</b> (1×, $100.00) is <b>Gas</b>" in text
    transport = next(btn for row in kb.inline_keyboard for btn in row if btn.text == "Transport")
    _press(b, 2, transport.callback_data, out)                                 # not theirs
    assert out[-1][0] == "answer"
    _press(b, 1, transport.callback_data, out)
    assert "✅ <b>SHELL</b> → Transport (1 transaction(s) updated)" in out[-1][0]
    assert b.db.rows("SELECT category FROM merchant_map WHERE owner='admin' AND key='SHELL'")[0]["category"] == "Transport"
    out = []
    _call(b, asyncio, _cmd(b, "spend"), 1, "/spend fix nothing-like-this", out)
    assert "No merchant matching" in out[-1][0]


# ---- reports ---------------------------------------------------------------------------------------

def _seed_txns(db, rows, owner="a", source="statement"):
    with db.tx() as c:
        c.executemany("INSERT INTO expense_txns(id, owner, bank, last4, date, amount, description, kind, bank_category, "
                      "category, source, merchant) VALUES (?,?,?,?,?,?,?,?,'',?,?,?)",
                      [(f"{owner}{source}{i}", owner, "chase", "1111", d, amt, desc, kind, cat, source, key)
                       for i, (d, amt, desc, kind, cat, key) in enumerate(rows)])


def test_periods():
    from app.features.expenses import reports as R
    today = date(2026, 10, 24)                                     # a Saturday: the 7 days ending Friday
    assert R.period("week", today) == R.Period("week", date(2026, 10, 17), date(2026, 10, 23))
    assert R.period("week", today).previous() == R.Period("week", date(2026, 10, 10), date(2026, 10, 16))
    today = date(2026, 10, 19)
    assert R.period("month", today) == R.Period("month", date(2026, 9, 1), date(2026, 9, 30))
    assert R.period("quarter", today) == R.Period("quarter", date(2026, 7, 1), date(2026, 9, 30))
    assert R.period("year", date(2027, 1, 2)) == R.Period("year", date(2026, 1, 1), date(2026, 12, 31))
    assert R.period("month", today, current=True) == R.Period("month", date(2026, 10, 1), date(2026, 10, 19))
    assert R.period("month", date(2026, 1, 1)).previous() == R.Period("month", date(2025, 11, 1), date(2025, 11, 30))
    assert R.period("quarter", date(2026, 1, 1)).label == "Q4 2025"


def test_subscription_detection_and_price_increase():
    from app.features.expenses import recurring
    rows = [{"merchant": "NETFLIX", "category": "Subscriptions", "date": d, "amount": a}
            for d, a in [("2026-06-15", 1549), ("2026-07-15", 1549), ("2026-08-15", 1549), ("2026-09-15", 1799)]]
    rows += [{"merchant": "SAFEWAY", "category": "Groceries", "date": f"2026-0{m}-03", "amount": 5000} for m in range(6, 10)]
    rows += [{"merchant": "DOMAIN CO", "category": "Other", "date": d, "amount": 1200} for d in ("2025-09-20", "2026-09-19")]
    rows += [{"merchant": "OLD GYM", "category": "Health", "date": f"2026-0{m}-01", "amount": 3000} for m in range(1, 5)]
    subs = {s.merchant: s for s in recurring.detect(rows, date(2026, 10, 1))}
    assert set(subs) == {"NETFLIX", "DOMAIN CO"}                   # groceries skipped; gym ended in April
    n = subs["NETFLIX"]
    assert (n.cadence, n.amount_cents, n.previous_cents, n.increased, n.next_due()) == ("monthly", 1799, 1549, True, date(2026, 10, 15))
    assert subs["DOMAIN CO"].cadence == "yearly" and subs["DOMAIN CO"].monthly_cents == 100


def test_alert_matching_exact_tip_and_dropped():
    from app.features.expenses import store
    alerts = [("x1", date(2026, 10, 3), 4000), ("x2", date(2026, 10, 4), 2000), ("x3", date(2026, 10, 5), 999)]
    lines = [(date(2026, 10, 4), 4000, "Groceries"), (date(2026, 10, 5), 2400, "Dining")]
    assert store.match_alerts(alerts, lines) == {"confirmed": 1, "tip": 1, "dropped": 1}


def test_statement_replaces_alert_charges_in_its_window(tmp_path, monkeypatch):
    from app.features.expenses import store
    b, _ = _bot(tmp_path, monkeypatch)
    with b.db.tx() as c:
        c.executemany("INSERT INTO expense_txns(id, owner, bank, last4, date, amount, description, kind, bank_category, "
                      "category, source) VALUES (?,?,?,?,?,?,?,?,'','','alert')",
                      [("al1", "admin", "boa", "4321", "2026-09-12", 4550, "SAFEWAY #1234", "purchase"),
                       ("al2", "admin", "boa", "4321", "2026-10-20", 999, "LATER", "purchase")])   # after the period: kept
    st = S.parse(BOA, BOA_FIRST)
    assert store.preview_matches(b.db, "admin", st) == {"confirmed": 1, "tip": 0, "dropped": 0}
    store.import_statement(b.db, "admin", st)
    ids = {r["id"] for r in b.db.rows("SELECT id FROM expense_txns WHERE source='alert'")}
    assert ids == {"al2"}


def test_monthly_report_compares_lists_subs_and_charts(tmp_path, monkeypatch):
    from app.features.expenses import reports as R
    b, _ = _bot(tmp_path, monkeypatch)
    rows = []
    for m, groceries in ((6, 30000), (7, 32000), (8, 34000), (9, 50000)):
        rows += [(f"2026-{m:02d}-05", groceries, "SAFEWAY #1", "purchase", "Groceries", "SAFEWAY"),
                 (f"2026-{m:02d}-15", 1549 if m < 9 else 1799, "NETFLIX.COM", "purchase", "Subscriptions", "NETFLIX"),
                 (f"2026-{m:02d}-20", 6000, "SQ *BLUE DOOR CAFE #12", "purchase", "Dining", "BLUE DOOR")]
    rows += [("2026-09-28", -2000, "SAFEWAY REFUND", "credit", "Groceries", "SAFEWAY"),
             ("2026-09-29", -50000, "PAYMENT THANK YOU", "payment", "", "")]
    _seed_txns(b.db, rows, owner="admin")
    with b.db.tx() as c:
        c.execute("INSERT INTO expense_statements VALUES ('s1','admin','chase','1111','2026-09-10','2026-10-09',0,0,'x')")
    asked = []
    rep = R.build(b.db, "admin", "month", date(2026, 10, 1), {}, lambda p: asked.append(p) or "• Groceries jumped.\n• Tip.")
    t = rep.text
    assert t.startswith("🧾 <b>September 2026</b> · <b>$557.99</b> (4 items)")      # refund counted, payment not
    assert "▲" in t and "vs Aug" in t and "3-month avg" in t
    assert "• Groceries $480.00 (86%) ▲ $140" in t and "• Blue Door $60.00 (1×)" in t
    assert "<b>Subscriptions</b>" in t and "Netflix $17.99/mo ⚠️ up from $15.49" in t
    assert "<b>Takeaways</b>" in t and "Groceries jumped" in t
    assert "SAFEWAY" not in asked[0] and "Groceries: 480" in asked[0]           # aggregated numbers only
    assert rep.chart.startswith(b"\x89PNG") and rep.caption == "🧾 September 2026 · $557.99"
    assert "Not complete" not in t                                              # statement covers September


def _alert_rows(db, dates, last4="1111", owner="admin"):
    with db.tx() as c:
        c.executemany("INSERT INTO expense_txns(id, owner, bank, last4, date, amount, description, kind, bank_category, "
                      "category, source, merchant) VALUES (?,?,?,?,?,?,?,'purchase','','Other','alert','X')",
                      [(f"al{last4}{d}", owner, "chase", last4, d, 100, "X") for d in dates])


def test_week_report_and_notes(tmp_path, monkeypatch):
    from app.features.expenses import reports as R
    b, _ = _bot(tmp_path, monkeypatch)
    _seed_txns(b.db, [("2026-10-13", 2500, "SHELL 99", "purchase", "Gas", "SHELL")], owner="admin")
    with b.db.tx() as c:
        c.execute("INSERT INTO expense_statements VALUES ('s1','admin','chase','1111','2026-09-15','2026-10-14',0,5000,'x')")
    t = R.build(b.db, "admin", "week", date(2026, 10, 17)).text      # Saturday; statement-based week
    assert t.startswith("📅 <b>Week of Oct 10–Oct 16</b> · <b>$25.00</b> (1 items)") and "Shell $25.00 (Tue)" in t
    assert "⚠️" not in t and "From card alerts" not in t                # no statement warnings at all
    month = R.build(b.db, "admin", "month", date(2026, 11, 1), {}).text  # no alerts on that card -> not counted later
    assert "Not counted after their last statement (no alerts): Chase ··1111." in month
    with b.db.tx() as c:                                                 # idle card ($0 balance): not listed
        c.execute("UPDATE expense_statements SET new=0")
    assert "Not counted" not in R.build(b.db, "admin", "month", date(2026, 11, 1), {}).text
    empty = R.build(b.db, "admin", "week", date(2026, 11, 2))
    assert empty.empty and "no card activity recorded. Latest statement ends Oct 14." in empty.text


def test_alert_notes_footer_and_silent_card(tmp_path, monkeypatch):
    from app.features.expenses import reports as R
    b, _ = _bot(tmp_path, monkeypatch)
    _alert_rows(b.db, ["2026-08-20", "2026-09-01", "2026-09-10", "2026-09-20"])          # a habit, then silence
    _alert_rows(b.db, ["2026-10-20"], last4="2222")                                     # one alert: no habit
    lines = R.notes(b.db, "admin", R.period("week", date(2026, 10, 24)), date(2026, 10, 24))
    assert lines == ["⚠️ No alerts from Chase ··1111 since Sep 20. Check that card's alert setting.",
                     "<i>From card alerts: tips and refunds may be missing until a statement is imported.</i>"]
    early = R.notes(b.db, "admin", R.period("week", date(2026, 9, 26)), date(2026, 9, 26))
    assert not any("No alerts" in x for x in early)                                    # only 6 days quiet then


def test_statement_reminder_and_skip(tmp_path, monkeypatch):
    from datetime import timedelta

    from helpers import _RecNotifier

    from app.features.expenses import feature as F
    b, _ = _bot(tmp_path, monkeypatch)
    s = b.services
    s.n = _RecNotifier(s.db)
    today = [date(2026, 10, 24)]
    monkeypatch.setattr(s, "today", lambda: today[0])
    with s.db.tx() as c:
        c.execute("INSERT INTO expense_statements VALUES ('s1','admin','chase','1111','2026-06-01','2026-06-30',0,5000,"
                  "'2026-07-05T10:00:00+00:00')")
    _alert_rows(s.db, ["2026-10-20"], last4="1111")
    _alert_rows(s.db, ["2026-10-21"], last4="3333")                                      # alerts only, never a statement
    asyncio.run(F.run_statement_reminder(s))
    assert s.n.sent == [("admin", "🧾 <b>Statements, optional</b>: none imported for 3 months for Chase ··1111, Chase ··3333. "
                                  "Sending them corrects tips and adds refunds; card alerts keep reports going either way.")]
    asyncio.run(F.run_statement_reminder(s))
    assert len(s.n.sent) == 1                                                            # not again the next day
    today[0] += timedelta(days=15)
    asyncio.run(F.run_statement_reminder(s))
    assert len(s.n.sent) == 2                                                            # no answer: again after 2 weeks
    out = []
    q = SimpleNamespace(data="exp:skip:admin", message=SimpleNamespace(text_html="reminder"))

    async def answer(*a, **k):
        out.append(("answer", a))

    async def edit(text, **k):
        out.append((text, None))
    q.answer, q.edit_message_text = answer, edit
    asyncio.run(F.on_skip_button(b, SimpleNamespace(effective_chat=SimpleNamespace(id=1), callback_query=q), None))
    assert "Skipped. Next reminder around Feb 06" in out[-1][0]
    today[0] += timedelta(days=60)
    asyncio.run(F.run_statement_reminder(s))
    assert len(s.n.sent) == 2                                                            # skipped for 3 months
    asyncio.run(F.on_skip_button(b, SimpleNamespace(effective_chat=SimpleNamespace(id=2), callback_query=q), None))
    assert out[-1] == ("answer", ("Not yours.",))


def test_best_card_check(tmp_path, monkeypatch):
    from app.features.expenses import reports as R
    b, _ = _bot(tmp_path, monkeypatch)
    cards = {"frdm": {"name": "Freedom", "owner": "admin", "last4": ["2222"], "rates": {"other": 1},
                      "rotating": {"issuer": "chase", "rate": 5, "quarters": {"2026Q3": ["Grocery Stores"]}}},
             "flat": {"name": "Flat", "owner": "admin", "last4": ["1111"], "rates": {"other": 2}}}
    _seed_txns(b.db, [("2026-08-05", 10000, "SAFEWAY #1", "purchase", "Groceries", "SAFEWAY"),       # on Flat: 3% missed
                      ("2026-08-06", 10000, "SHELL", "purchase", "Gas", "SHELL")], owner="admin")      # Flat is best: fine
    lines, missed = R.card_check(b.db, cards, "admin", R.Period("quarter", date(2026, 7, 1), date(2026, 9, 30)))
    assert missed == 300 and "Freedom (2026Q3 5% category): $3.00" in lines[1]
    for c in cards.values():
        c.pop("last4")
    assert "Add <code>last4</code>" in R.card_check(b.db, cards, "admin", R.period("quarter", date(2026, 10, 1)))[0][0]


def test_display_names_and_rename(tmp_path, monkeypatch):
    from app.features.expenses import categories as C
    assert [C.pretty(k) for k in ("TRADER JOE'S", "AT&T", "CVS", "BLUE DOOR", "DOORDASH", "76")] == \
        ["Trader Joe's", "AT&T", "CVS", "Blue Door", "DoorDash", "76"]
    b, _ = _bot(tmp_path, monkeypatch)
    _seed_txns(b.db, [("2026-10-01", 500, "SQ *BLUE DOOR CAFE", "purchase", "Dining", "BLUE DOOR")], owner="admin")
    out = []
    _call(b, asyncio, _cmd(b, "spend"), 1, "/spend name blue door = Blue Door Cafe", out)
    assert "now shows as <b>Blue Door Cafe</b>" in out[-1][0] and C.display_name(b.db, "admin", "BLUE DOOR") == "Blue Door Cafe"
    assert C.display_name(b.db, "sci", "BLUE DOOR") == "Blue Door"                 # per person


def test_scheduled_report_goes_only_to_people_with_data(tmp_path, monkeypatch):
    from helpers import _RecNotifier

    from app.features.expenses import feature as F
    b, _ = _bot(tmp_path, monkeypatch)
    b.services.n = _RecNotifier(b.db)
    monkeypatch.setattr(b.services, "today", lambda: date(2026, 10, 19))
    asyncio.run(F.run_report(b.services, "week"))
    assert b.services.n.sent == []                                             # nobody has imported anything
    _seed_txns(b.db, [("2026-10-13", 2500, "SHELL 99", "purchase", "Gas", "SHELL")], owner="admin")
    asyncio.run(F.run_report(b.services, "week"))
    assert [p for p, _ in b.services.n.sent] == ["admin"]


AMEX_FIRST = ("Blue Amex Card\nClosing Date 10/02/26\nAccount Ending 1-55555\nAmerican Express\n"
              "Previous Balance $50.00\nNew Balance $65.00")
AMEX = _words([_row(40, "Payments and Credits"),
               _row(40, "Summary"),
               _row(40, "Total Payments and Credits -$50.00"),
               _row(40, "Detail"),
               _row(40, "Payments Amount"),
               _row(40, "09/20/26* MOBILE PAYMENT RECEIVED - THANK YOU -$50.00"),
               _row(40, "New Charges"),
               _row(40, "Card Ending 1-55555"),
               _row(40, "09/05/26 WHOLE FOODS MARKET SPRINGFIELD CA $60.00"),
               _row(40, "8005551234"),
               _row(40, "10/01/26 NETFLIX.COM $5.00"),
               _row(40, "Fees"),
               _row(40, "Total Fees for this Period $0.00"),
               _row(40, "Days in Billing Period: 30")])


def test_amex_layout_dates_with_year_five_digit_ending_and_derived_period():
    st = S.parse(AMEX, AMEX_FIRST)
    assert (st.bank, st.last4, st.start, st.end) == ("amex", "55555", date(2026, 9, 3), date(2026, 10, 2))
    assert [(t.date, t.kind, t.amount_cents) for t in st.txns] == [
        (date(2026, 9, 20), "payment", -5000), (date(2026, 9, 5), "purchase", 6000), (date(2026, 10, 1), "purchase", 500)]
    assert st.ok


# ---- alert emails ----------------------------------------------------------------------------------

BOA_ALERT = """<html><body><table><tr><td>Credit card transaction<br>exceeds alert limit you set</td></tr>
<tr><td>Unlimited Cash Rewards Visa Signature ending in 4321</td></tr>
<tr><td>Amount:</td></tr><tr><td>$10.89</td></tr><tr><td>Date:</td></tr><tr><td>October 19, 2026</td></tr>
<tr><td>Where:</td></tr><tr><td>USPS PO 0123456789</td></tr><tr><td><a href="https://x">View details</a></td></tr>
<tr><td>Bank of America, N.A. Member FDIC.</td></tr></table></body></html>"""


def test_boa_alert_direct_and_forwarded():
    from app.features.expenses import alerts as A
    a = A.parse(BOA_ALERT)
    assert a == A.Alert("boa", "4321", "Unlimited Cash Rewards Visa Signature", date(2026, 10, 19), 1089, "USPS PO 0123456789")
    fwd = "<div>---------- Forwarded message ---------<br>From: Bank of America &lt;onlinebanking@ealerts.example&gt;</div>" + BOA_ALERT
    assert A.parse(fwd) == a
    assert A.parse(BOA_ALERT.replace("transaction<br>exceeds alert limit", "payment received")) is None   # not a charge
    assert A.parse("<p>Amount: $5.00 Date: October 1, 2026</p>") is None                                   # unknown bank


def _mail(mid, html, sender="Bank of America <onlinebanking@ealerts.example>"):
    from app.integrations.gmail import Mail
    return Mail(mid, "Credit card transaction exceeds alert limit you set", sender, html)


def test_alert_emails_become_charges_until_a_statement_covers_them(tmp_path, monkeypatch):
    from app.features.expenses import feature as F
    b, _ = _bot(tmp_path, monkeypatch)
    s = b.services
    asyncio.run(F.handle_mail(s, [_mail("<m1>", BOA_ALERT), _mail("<m1>", BOA_ALERT), _mail("<m2>", "<p>hello</p>")]))
    rows = s.db.rows("SELECT * FROM expense_txns")
    assert [(r["owner"], r["bank"], r["last4"], r["date"], r["amount"], r["source"], r["category"]) for r in rows] == \
        [("admin", "boa", "4321", "2026-10-19", 1089, "alert", "Other")]          # only expenses person; dup ignored
    assert {"<m1>", "<m2>"} <= s.db.processed_email_ids()
    with s.db.tx() as c:                                                           # statement covering Oct 19 arrives
        c.execute("INSERT INTO expense_statements VALUES ('st','admin','boa','4321','2026-10-10','2026-11-09',0,0,'x')")
    asyncio.run(F.handle_mail(s, [_mail("<m3>", BOA_ALERT.replace("$10.89", "$5.00"))]))
    assert s.db.rows("SELECT count(*) n FROM expense_txns")[0]["n"] == 1             # covered: statement wins


def test_alert_owner_by_card_then_forwarder(tmp_path, monkeypatch):
    from app.features.expenses import alerts as A
    from app.features.expenses import feature as F
    b, _ = _bot(tmp_path, monkeypatch)
    s = b.services
    a = A.parse(BOA_ALERT)
    s.cfg.profiles["sci"]["features"] = ["expenses"]                               # now two people have expenses
    s.cfg.profiles["sci"]["email_from"] = ["sci@example.com"]
    assert F.alert_owner(s, a, "Bank of America <x@bank.example>") is None          # ambiguous: skipped
    assert F.alert_owner(s, a, "Sci <sci@example.com>") == "sci"                   # forwarded by sci
    s.cfg.cards["disc"]["last4"] = ["4321"]                                        # a Discover card with those digits
    assert F.alert_owner(s, a, "Sci <sci@example.com>") == "sci"                   # ...isn't this BoA card
    s.cfg.cards["boa1"] = {"name": "BoA Cash", "owner": "admin", "last4": ["4321"]}  # the BoA card's owner wins
    assert F.alert_owner(s, a, "Sci <sci@example.com>") == "admin"


def test_alert_search_terms():
    from app.features.expenses import alerts as A
    assert A.imap_criteria(["a"]) == ["SUBJECT", '"a"']
    assert A.imap_criteria(["a", "b", "c"]) == ["OR", "SUBJECT", '"a"', "OR", "SUBJECT", '"b"', "SUBJECT", '"c"']


CHASE_ALERT = """<html><body><table><tr><td>This transaction is above the level you set, see more here.</td></tr>
<tr><td>Transaction alert</td></tr><tr><td>You made a $28.37 transaction</td></tr>
<tr><td>Account</td></tr><tr><td>Chase Freedom Visa (...8765)</td></tr>
<tr><td>Date</td></tr><tr><td>Oct 24, 2026 at 2:54 PM ET</td></tr>
<tr><td>Merchant</td></tr><tr><td>Blue Door Cafe LLC</td></tr>
<tr><td>Amount</td></tr><tr><td>$28.37</td></tr>
<tr><td>If you didn't make this purchase, visit chase.com/alerts.</td></tr>
<tr><td>© 2026 JPMorgan Chase &amp; Co.</td></tr></table></body></html>"""


def test_chase_alert_direct_and_forwarded():
    from app.features.expenses import alerts as A
    a = A.parse(CHASE_ALERT)
    assert a == A.Alert("chase", "8765", "Chase Freedom Visa", date(2026, 10, 24), 2837, "Blue Door Cafe LLC")
    fwd = ("<div>From: Someone &lt;me@example.com&gt;<br>Sent: Saturday, October 24, 2026<br>"
           "Subject: You made a $28.37 transaction with Blue Door Cafe LLC</div>" + CHASE_ALERT)
    assert A.parse(fwd) == a
    assert A.parse(CHASE_ALERT.replace("Transaction alert", "Payment alert")) is None        # not a charge
    assert A.parse(BOA_ALERT).bank == "boa"                    # BoA's "purchase" text isn't mistaken for Chase
    assert A.imap_criteria(A.SUBJECTS) == ["OR", "SUBJECT", '"transaction exceeds alert limit"', "SUBJECT", '"transaction with"']


def test_card_matching_needs_the_same_bank():
    from app.features.expenses import store
    cards = {"a": {"name": "Chase Freedom", "owner": "x", "last4": ["1234"]},
             "b": {"name": "Some Card", "owner": "y", "last4": ["1234"], "bank": "discover"},
             "c": {"name": "Plain", "owner": "z", "last4": ["5555"]}}
    assert store.card_bank(cards["a"]) == "chase" and store.card_bank(cards["c"]) is None
    assert store.find_card(cards, "chase", "1234")["owner"] == "x"
    assert store.find_card(cards, "discover", "1234")["owner"] == "y"      # same digits, other bank
    assert store.find_card(cards, "boa", "1234") is None
    assert store.find_card(cards, "boa", "5555")["owner"] == "z"            # bank unknown in config: digits decide
