"""Core tests."""
from datetime import date

import pytest
from helpers import (
    _fake_client,
    _FakeClock,
    _FakeIMAP,
)

from app.db import DB
from app.features.placeholder import placeholder
from app.notify import split_message


def test_processed_emails_and_kv():
    db = DB(":memory:")
    db.mark_emails_processed(["<a@x>", "<b@x>"])
    db.mark_emails_processed(["<a@x>"])
    assert db.processed_email_ids() == {"<a@x>", "<b@x>"}
    db.set_kv("k", "v")
    assert db.get_kv("k") == "v" and db.get_kv("missing") is None


def test_split_message():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = split_message(text, 1000)
    assert all(len(c) <= 1000 for c in chunks) and "\n".join(chunks) == text


def test_placeholder_renders_png():
    png = placeholder.render(date(2026, 9, 1), "Monthly parking", "$100.00")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000
    assert placeholder.period_for(date(2026, 2, 1)) == (date(2026, 2, 1), date(2026, 2, 28))


def test_deep_merge_layers():
    from app.config import deep_merge
    base = {"a": {"x": 1, "y": 2}, "zips": {"94103": "SF"}, "lst": [1, 2], "gone": 1}
    local = {"a": {"y": 3}, "zips": {"_replace": True, "94110": "San Francisco"}, "lst": [9], "gone": None}
    assert deep_merge(base, local) == {"a": {"x": 1, "y": 3}, "zips": {"94110": "San Francisco"}, "lst": [9]}


def test_breaker_skips_failing_model_then_reprobes():
    from app.integrations import llm
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
    from app.integrations import llm
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
    from app.integrations import llm
    clock = _FakeClock()
    h = llm.ModelHealth(clock=clock)
    h.record_fail("a", 503)
    h.record_fail("b", 503)
    calls = []
    out = llm.summarize("k", ["a", "b"], "p", attempts_per_model=3, health=h,
                        _client_factory=_fake_client({"a": 503, "b": "ok"}, calls), _sleep=lambda s: None)
    assert out == "answer from b" and calls == ["a", "b"]  # last resort, one probe each, no retry storm


def test_error_alerts_throttled():
    import asyncio
    from types import SimpleNamespace

    from app.services import Services
    sent = []

    class N:
        async def send_text(self, profiles, text):
            sent.append(text)
    cfg = SimpleNamespace(profiles={"me": {"primary": True}}, primary_profile="me")
    j = Services.__new__(Services)
    j.cfg, j.db, j.n = cfg, DB(":memory:"), N()
    for _ in range(3):
        asyncio.run(j.report_error("realestate", RuntimeError("imap down")))
    asyncio.run(j.report_error("market", RuntimeError("yahoo down")))
    assert len(sent) == 2 and "realestate" in sent[0] and "market" in sent[1]


def test_gmail_session_headers_first(monkeypatch):
    from app.integrations import gmail
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


def test_prune_retention_rules(tmp_path):
    import json
    import time as _t

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
    assert out == {"emails": 1, "listings": 1, "biorxiv": 0, "re_events": 0, "job_runs": 0, "deliveries": 0, "quarters": 2, "err_alerts": 1,
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
    db.set_kv("health:fail:gmail", "{}"); db.set_kv("alarm:job:gmail", "1"); db.set_kv("alarm:disk", "1")
    assert mt.one_time_cleanup(db) == {"legacy_listings": 1, "offers_table": 1, "legacy_job_health": 2}
    assert mt.one_time_cleanup(db) == {"legacy_listings": 0, "offers_table": 0, "legacy_job_health": 0}  # idempotent
    assert db.get_kv("alarm:disk") == "1"                                                   # VM alarm state kept
    for _ in range(3):
        mt.backup(db, tmp_path / "bk", keep=2)
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
