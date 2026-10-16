"""Registry tests: the core is feature-agnostic, so a new feature needs no edits outside its package."""
import asyncio

from helpers import _bot, _call, _cmd


def test_names_unique_and_schedules_parse(tmp_path, monkeypatch):
    from apscheduler.triggers.cron import CronTrigger

    from app.features import ALL
    b, _ = _bot(tmp_path, monkeypatch)
    names = [c.name for f in ALL for c in f.commands] + [a.name for f in ALL for a in f.aliases]
    assert len(names) == len(set(names)) and not {"help", "features", "status", "run", "start"} & set(names)
    assert len({f.name for f in ALL}) == len(ALL)
    jobs = b.services.jobs()
    assert {"mail", "cleanup", "backup", "market", "evening", "mortgage", "realestate", "cards", "cardmail",
            "activation", "papers", "health", "placeholder"} == set(jobs)
    for _fn, spec in jobs.values():
        if spec:
            (CronTrigger.from_crontab(spec) if isinstance(spec, str) else CronTrigger(**spec))  # must not raise
    b._schedule()
    ids = {j.id for j in b.scheduler.get_jobs()}
    assert {"mail", "cleanup", "backup", "health", "papers:sci"} <= ids   # per-person bioRxiv job from the registry


def test_new_feature_plugs_in_without_core_changes(tmp_path, monkeypatch):
    from app import features
    from app.registry import Alias, Command, Feature, Job
    ran = []

    async def hello(bot, update, ctx):
        await bot._reply(update, "hello from a plugged-in feature")

    async def job(s, force=False):
        ran.append(force)

    demo = Feature(name="demo", description="A demo feature", commands=(Command("hello", "Say hello", hello, ("/hello",)),),
                   aliases=(Alias("hi", hello, "/hello"),), jobs=(Job("demo", job, lambda st: {"hour": 6}),))
    monkeypatch.setattr(features, "ALL", features.ALL + [demo])
    b, _ = _bot(tmp_path, monkeypatch)
    assert "/hello — Say hello" in b._help("admin") and "hello" in [c for c, _, _ in b._commands_for("admin")]
    assert "hello" not in [c for c, _, _ in b._commands_for("sci")]     # sci's features list doesn't include demo
    out = []
    _call(b, asyncio, _cmd(b, "hello"), 1, "/hello", out)
    _call(b, asyncio, _cmd(b, "hi"), 1, "/hi", out)                     # alias works + tip
    assert out[0][0] == "hello from a plugged-in feature" and "Tip: /hi is now /hello" in out[-1][0]
    asyncio.run(b.services.run_job("demo"))
    assert ran == [True] and "demo" in b.services.jobs()
    text, _kb = b._features_view("admin")
    assert "<b>demo</b>: A demo feature" in text
