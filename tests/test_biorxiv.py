"""Biorxiv tests."""

from helpers import _paper, _RecNotifier

from app.db import DB
from app.features.market import brief


def test_biorxiv_keyword_matching():
    from app.features.biorxiv import biorxiv as bx
    p = _paper("10.1/a", "A Genome Wide atlas of the human Cortex", "We use organoid-culture ...")
    assert bx.match_keywords(p, ["cortex", "genome-wide", "organoid culture", "zebrafish"]) == \
        ["cortex", "genome-wide", "organoid culture"]
    assert bx.match_keywords(_paper("10.1/b", "Cortexin protein family"), ["cortex"]) == []   # whole word only


def test_biorxiv_fetch_pages_filters_categories_and_revisions():
    from datetime import date as d

    from app.features.biorxiv import biorxiv as bx
    pages = {0: [{"doi": "10.1/a", "title": "A", "category": "neuroscience", "version": "1", "date": "2026-10-14"},
                 {"doi": "10.1/b", "title": "B", "category": "microbiology", "version": "1", "date": "2026-10-14"}],
             2: [{"doi": "10.1/c", "title": "C", "category": "Cell Biology", "version": "2", "date": "2026-10-14"},
                 {"doi": "10.1/d", "title": "D", "category": "cell biology", "version": "1", "date": "2026-10-15"},
                 {"doi": "10.1/a", "title": "A", "category": "neuroscience", "version": "1", "date": "2026-10-14"}]}
    calls = []

    def get(url):
        cursor = int(url.rsplit("/", 1)[1]); calls.append(cursor)
        coll = pages.get(cursor, [])
        return {"collection": coll, "messages": [{"total": 5, "count": len(coll)}]}
    got = bx.fetch_papers(d(2026, 10, 13), d(2026, 10, 15), ["cell_biology", "neuroscience"], get=get)
    assert [p.doi for p in got] == ["10.1/a", "10.1/d"] and calls == [0, 2]      # paged; revision + other cat + dupe dropped
    assert [p.doi for p in bx.fetch_papers(d(2026, 10, 13), d(2026, 10, 15), ["cell biology"], True, get)] == ["10.1/c", "10.1/d"]


def test_biorxiv_select_dedupes_and_orders():
    from app.features.biorxiv import biorxiv as bx
    ps = [_paper("10.1/old", "Cortex study", posted="2026-10-13"),
          _paper("10.1/new", "Cortex study two", posted="2026-10-14"),
          _paper("10.1/both", "Genome-wide cortex atlas", posted="2026-10-13"),
          _paper("10.1/sent", "Cortex again"), _paper("10.1/none", "Liver")]
    got = bx.select(ps, ["cortex", "genome-wide"], already={"10.1/sent"})
    assert [p.doi for p in got] == ["10.1/both", "10.1/new", "10.1/old"]
    # keyword order is priority: one core-topic hit outranks two lower-priority method hits
    ps2 = [_paper("10.1/methods", "Genome-wide organoid culture of tumors"), _paper("10.1/core", "Cortex development")]
    got2 = bx.select(ps2, ["cortex", "zebrafish", "genome-wide", "organoid culture"], set())
    assert [p.doi for p in got2] == ["10.1/core", "10.1/methods"]


def test_biorxiv_summaries_and_digest():
    from datetime import date as d

    from app.features.biorxiv import biorxiv as bx
    a, b = _paper("10.1/a", "Cortex <atlas>", "Long abstract " * 40), _paper("10.1/b", "Another cortex paper", "Text " * 80)
    a.matched, b.matched = ["cortex"], ["cortex"]
    prompt = bx.build_prompt([a, b])
    assert "3 concise bullet points" in prompt and "Goal: " in prompt and "### id: 10.1/a" in prompt
    s = bx.parse_summaries('```json\n{"papers": [{"id": "10.1/a", "bullets": ["目标：x", "方法：y", "发现：z", "extra"]}]}\n```')
    assert s == {"10.1/a": ["目标：x", "方法：y", "发现：z"]}
    out = bx.format_digest([a, b], s, [_paper("10.1/c", "Extra cortex")], d(2026, 10, 13), d(2026, 10, 15))
    assert "📚 <b>New on bioRxiv</b> · 10/13–10/15 · 3 papers" in out and "Cortex &lt;atlas&gt;" in out
    assert "• 目标：x" in out and "Text Text" in out and "More matches (1)" in out   # b has no summary: abstract fallback
    assert 'href="https://www.biorxiv.org/content/10.1/av1"' in out
    assert "AI summaries unavailable" in bx.format_digest([a], {}, [], d(2026, 10, 15), d(2026, 10, 15))


def test_biorxiv_job_waits_for_registration_and_marks_after_send(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app.features.biorxiv import biorxiv as bx
    from app.features.biorxiv import feature as bxf
    from app.services import Services
    db = DB(":memory:")
    monkeypatch.setattr(bx, "fetch_papers", lambda *a, **k: [_paper("10.1/a", "Cortex atlas")])
    cfg = SimpleNamespace(settings={}, cards={}, gemini_api_key="", primary_profile="admin", tz=brief.ZoneInfo("America/Los_Angeles"),
                          profiles={"admin": {"primary": True}, "b": {"features": ["biorxiv"], "biorxiv": {"keywords": ["cortex"]}}})
    n = _RecNotifier(db)
    j = Services(cfg, db, n)
    assert bxf.profiles(j) == ["b"]
    asyncio.run(bxf.run(j))
    assert n.sent == [] and db.biorxiv_sent("b") == set()                 # b not registered: nothing marked
    db.register_user(2, "b", "B")
    asyncio.run(bxf.run(j))
    assert [t for _, t in n.sent][0].startswith("📚 <b>New on bioRxiv</b>") and db.biorxiv_sent("b") == {"10.1/a"}
    n.sent.clear()
    asyncio.run(bxf.run(j))
    assert n.sent == []                                                    # already sent: silent


def test_biorxiv_alternative_spellings_plurals_and_priority():
    from app.features.biorxiv import biorxiv as bx
    kws = ["NPC|neural progenitor cell", "gut regeneration|intestinal regeneration", "cell-cycle|cellcycle|mitosis"]
    p = _paper("10.1/a", "Neural progenitor cells drive intestinal regeneration", "NPCs and mitosis of tissue")
    assert bx.match_keywords(p, kws) == ["NPC", "gut regeneration", "cell-cycle"]       # labels, plurals ok
    assert bx.match_keywords(_paper("10.1/b", "Cell cycle screen"), kws) == ["cell-cycle"]  # hyphen/space same
    assert bx.match_keywords(_paper("10.1/c", "NPCX protein"), kws) == []                   # whole word (+s only)
    hi, lo = _paper("10.1/hi", "NPC study"), _paper("10.1/lo", "Mitosis study")
    assert [x.doi for x in bx.select([lo, hi], kws, set())] == ["10.1/hi", "10.1/lo"]     # first keyword ranks first


def test_biorxiv_keywords_edited_in_bot_override_config():
    from types import SimpleNamespace

    from app.features.biorxiv import feature as bxf
    from app.services import Services
    cfg = SimpleNamespace(settings={}, cards={}, gemini_api_key="", primary_profile="admin",
                          profiles={"admin": {"primary": True}, "b": {"features": ["biorxiv"], "biorxiv": {"keywords": ["cortex"]}}})
    j = Services(cfg, DB(":memory:"), None)
    assert bxf.terms(j, "b") == ["cortex"]
    bxf.set_terms(j, "b", ["cortex", "zebrafish|danio"])
    assert bxf.terms(j, "b") == ["cortex", "zebrafish|danio"]
    bxf.set_terms(j, "b", None)
    assert bxf.terms(j, "b") == ["cortex"]


def test_biorxiv_context_terms_required():
    from app.features.biorxiv import biorxiv as bx
    kws, ctx = ["NPC|neural progenitor cell", "gut regeneration"], ["zebrafish|danio", "fin"]
    ps = [_paper("10.1/in", "NPC maintenance in zebrafish"),                                  # keyword + context
          _paper("10.1/out", "NPC maintenance in mouse cortex"),                              # keyword, no context
          _paper("10.1/ctx", "Zebrafish fin study"),                                          # context, no keyword
          _paper("10.1/abs", "Gut regeneration", "We study regenerating fins after injury")]  # context in abstract (+s)
    assert [p.doi for p in bx.select(ps, kws, set(), ctx)] == ["10.1/in", "10.1/abs"]
    assert {p.doi for p in bx.select(ps, kws, set())} == {"10.1/in", "10.1/out", "10.1/abs"}   # no context: as before


def test_biorxiv_quiet_week_recap(monkeypatch):
    import asyncio
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from app.features.biorxiv import biorxiv as bx
    from app.features.biorxiv import feature as bxf
    from app.services import Services
    db = DB(":memory:")
    sent_paper = _paper("10.1/sent", "Cortex wiring in zebrafish", "cortex of danio")
    near = _paper("10.1/near", "Neurogenesis in flies", "neurogenic fasting")       # keyword, no context
    papers = [sent_paper, near, _paper("10.1/off", "Unrelated kidney paper")]
    monkeypatch.setattr(bx, "fetch_papers", lambda *a, **k: papers)
    cfg = SimpleNamespace(settings={}, cards={}, gemini_api_key="", primary_profile="admin", tz=brief.ZoneInfo("America/Los_Angeles"),
                          profiles={"admin": {"primary": True},
                                    "b": {"features": ["biorxiv"], "biorxiv": {"keywords": ["cortex", "neurogenesis|neurogenic"],
                                                                                "context": ["zebrafish|danio"]}}})
    n = _RecNotifier(db)
    j = Services(cfg, db, n)
    db.register_user(2, "b", "B")
    with db.tx() as c:                     # a paper sent 8 days ago, stored before titles were kept
        c.execute("INSERT INTO biorxiv_history(profile, doi, sent) VALUES ('b', '10.1/sent', ?)",
                  ((datetime.now(UTC) - timedelta(days=8)).isoformat(),))
    assert bxf.recap_due(j, "b")
    text = bxf.build_recap(j, "b")
    assert "📚 <b>bioRxiv recap</b> · past 14 days" in text and "Sent to you (1)" in text
    assert 'href="https://www.biorxiv.org/content/10.1/sentv1">Cortex wiring in zebrafish' in text   # title filled in
    assert "Near misses (1)" in text and "Neurogenesis in flies" in text and "kidney" not in text
    asyncio.run(bxf.run(j))                # nothing new (sent one is deduped, near miss lacks context) -> recap
    assert [t for _, t in n.sent] == ["📚 <b>bioRxiv recap</b> · past 14 days"]
    asyncio.run(bxf.run(j))                # next day: no second recap
    assert len(n.sent) == 1 and not bxf.recap_due(j, "b")
    assert bxf.recap_due(j, "b", now=datetime.now(UTC) + timedelta(days=7))


def test_biorxiv_recap_when_nothing_matched():
    from app.features.biorxiv import biorxiv as bx
    text = bx.format_recap([], [], 14, 7, ["cortex|cortical"], ["zebrafish|danio"])
    assert "nothing matched your topics in the past 14 days" in text and "Keywords:</i> cortex" in text
    assert "Must also mention:</i> zebrafish" in text and "/papers add" in text


def test_biorxiv_history_gets_title_columns_on_old_databases(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE biorxiv_history (profile TEXT NOT NULL, doi TEXT NOT NULL, sent TEXT NOT NULL, PRIMARY KEY (profile, doi))")
        c.execute("INSERT INTO biorxiv_history VALUES ('b', '10.1/x', '2026-10-01')")
    db = DB(p)
    db.mark_biorxiv_sent("b", [("10.1/y", "T", "https://u")])
    assert [tuple(r) for r in db.biorxiv_sent_since("b", "2000")][-1] == ("10.1/x", "2026-10-01", "", "")
