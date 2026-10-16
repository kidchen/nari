"""Bot tests."""

from helpers import _bot, _call, _Chat, _cmd

from app.db import DB
from app.features.biorxiv import feature as bxf


def test_user_routing():
    db = DB(":memory:")
    db.register_user(111, "user_a", "A")
    db.register_user(222, "user_b", "B")
    db.register_user(111, "user_a", "A2")  # re-register is idempotent
    assert sorted(db.chat_ids_for(["user_a", "user_b"])) == [111, 222]
    assert db.chat_ids_for(["user_b"]) == [222]


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


def test_admin_only_features():
    from app.access import Access
    db = DB(":memory:")
    a = Access({"admin": {"primary": True, "features": "all"}, "other": {"features": "all"},
                "listed": {"features": ["realestate", "placeholder"]}}, db)
    assert "placeholder" in a.allowed("admin")
    assert "placeholder" not in a.allowed("other") and a.allowed("listed") == ["realestate"]
    assert a.recipients("placeholder") == ["admin"]


def test_command_menus_are_short_and_only_show_ready_features(tmp_path, monkeypatch):
    b, _ = _bot(tmp_path, monkeypatch)
    admin = [c for c, _, _ in b._commands_for("admin")]
    sci = [c for c, _, _ in b._commands_for("sci")]
    assert admin == ["help", "features", "brief", "portfolio", "watch", "cards", "status", "run"]  # no biorxiv set up
    assert sci == ["help", "features", "papers"]                                                   # no cards owned
    help_text = b._help("sci")
    assert "/papers — New bioRxiv papers" in help_text and "    /papers add zebrafish|danio" in help_text
    assert "/pause" not in b._help("admin") and "/topics" not in help_text


def test_papers_subcommands_and_old_topics_alias(tmp_path, monkeypatch):
    b, asyncio = _bot(tmp_path, monkeypatch)
    out = []
    _call(b, asyncio, _cmd(b, "papers"), 2, "/papers topics", out)
    assert "1. cortex" in out[-1][0] and "2. zebrafish|danio" in out[-1][0]
    _call(b, asyncio, _cmd(b, "papers"), 2, "/papers add gut regeneration|intestinal regeneration", out)
    assert "Added: gut regeneration|intestinal regeneration" in out[-1][0]
    _call(b, asyncio, _cmd(b, "papers"), 2, "/papers del 1", out)
    assert bxf.terms(b.services, "sci") == ["zebrafish|danio", "gut regeneration|intestinal regeneration"]
    _call(b, asyncio, _cmd(b, "topics"), 2, "/topics", out)                       # old command: works + points to new form
    assert "Tip: /topics is now /papers topics" in out[-1][0]
    _call(b, asyncio, _cmd(b, "papers"), 2, "/papers reset", out)
    assert bxf.terms(b.services, "sci") == ["cortex", "zebrafish|danio"]


def test_cards_subcommands_and_aliases(tmp_path, monkeypatch):
    b, asyncio = _bot(tmp_path, monkeypatch)
    out = []
    _call(b, asyncio, _cmd(b, "cards"), 1, "/cards fix disc Gas, Transit", out)
    assert "Saved" in out[-1][0] and "Gas, Transit" in out[-1][0] and "Tip" not in out[-1][0]
    _call(b, asyncio, _cmd(b, "cards"), 1, "/cards gas", out)
    assert "Disc Card — 5%" in out[-1][0]
    _call(b, asyncio, _cmd(b, "best"), 1, "/best gas", out)
    assert "Tip: /best is now /cards &lt;category&gt;" in out[-1][0]
    _call(b, asyncio, _cmd(b, "cards"), 2, "/cards fix disc X", out)              # sci has no cards: refused
    assert "isn't enabled" in out[-1][0] or "none" in out[-1][0]


def test_features_toggle_button(tmp_path, monkeypatch):
    from types import SimpleNamespace
    b, asyncio = _bot(tmp_path, monkeypatch)
    out = []
    _call(b, asyncio, b.features, 2, "/features", out)
    text, kb = out[-1]
    assert "✅ <b>realestate</b>" in text and [r[0].callback_data for r in kb.inline_keyboard] == ["feat:realestate", "feat:biorxiv"]

    class Q:
        data = "feat:realestate"
        async def answer(self, *a, **k): pass
        async def edit_message_text(self, text, **kw): out.append((text, kw.get("reply_markup")))
    asyncio.run(b.toggle_feature(SimpleNamespace(callback_query=Q(), effective_chat=_Chat(2, out)), None))
    assert b.services.access.paused("sci") == ["realestate"] and "⏸️ <b>realestate</b>" in out[-1][0]
    assert out[-1][1].inline_keyboard[0][0].text == "▶️ Resume realestate"
    _call(b, asyncio, b.resume, 2, "/resume realestate", out)            # old alias still works
    assert b.services.access.paused("sci") == [] and "Tip: /pause is now /features" in out[-1][0]


def test_papers_context_commands(tmp_path, monkeypatch):
    b, asyncio = _bot(tmp_path, monkeypatch)
    out = []
    _call(b, asyncio, _cmd(b, "papers"), 2, "/papers context add danio|zebrafish", out)
    assert "Added: danio|zebrafish" in out[-1][0] and "a paper must also mention" in out[-1][0]
    assert bxf.terms(b.services, "sci", "context") == ["danio|zebrafish"] and bxf.terms(b.services, "sci") == ["cortex", "zebrafish|danio"]
    _call(b, asyncio, _cmd(b, "papers"), 2, "/papers context del 1", out)
    assert bxf.terms(b.services, "sci", "context") == [] and "keywords alone decide" in out[-1][0]
    assert "Categories: neuroscience" in out[-1][0]
